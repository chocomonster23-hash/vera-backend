# VERA BACKEND - COMPLETE IMPLEMENTATION WITH ADMIN PANEL
# This is the complete app.py file - copy this entire code and upload to GitHub

import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict
import firebase_admin
from firebase_admin import db, auth as firebase_auth
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import stripe
import hashlib
from enum import Enum

load_dotenv()

# ============ INITIALIZATION ============

cred = firebase_admin.credentials.Certificate(json.loads(os.getenv('FIREBASE_KEY')))
firebase_admin.initialize_app(cred, {
    'databaseURL': os.getenv('FIREBASE_DATABASE_URL')
})

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
DEVELOPER_EMAIL = "o.maryna@hotmail.com"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============ PRICING ============

PRICING = {
    "tier_1": {
        "monthly": 5.99,
        "annual": 57.60,
        "lifetime": 99.00
    },
    "tier_2": {
        "monthly": 3.99,
        "annual": 38.40,
        "lifetime": 99.00
    },
    "tier_3": {
        "monthly": 1.99,
        "annual": 19.20,
        "lifetime": 99.00
    },
    "b2b_sales": {
        "monthly": 4.99,
        "annual": 47.90,
        "lifetime": 99.00
    }
}

TRIAL_DAYS = {
    "buyer": 0,
    "seller": 14
}

FREE_TIER_SAVES = 2

# ============ USER MANAGEMENT ============

def get_or_create_user(email: str, role: str, tier: str = "tier_1"):
    """Get existing user or create new one"""
    user_ref = db.reference(f'users/{email}')
    user_data = user_ref.get()
    
    if user_data:
        return user_data
    
    is_developer = email.lower() == DEVELOPER_EMAIL.lower()
    
    now = datetime.now()
    trial_end = None
    
    if role == "seller":
        trial_end = (now + timedelta(days=TRIAL_DAYS["seller"])).isoformat()
    
    new_user = {
        "email": email,
        "role": role,
        "tier": tier,
        "created_at": now.isoformat(),
        "subscription": "free" if role == "buyer" else None,
        "subscription_plan": None,
        "subscription_expires": None,
        "trial_expires": trial_end,
        "saved_calculations": 0,
        "is_developer": is_developer,
        "stripe_customer_id": None,
        "lifetime_purchased": False
    }
    
    user_ref.set(new_user)
    return new_user

def get_user(email: str) -> Dict:
    """Get user from Firebase"""
    user_data = db.reference(f'users/{email}').get()
    if not user_data:
        raise HTTPException(status_code=401, detail="User not found")
    return user_data

def check_subscription_access(user: Dict, role: str) -> bool:
    """Check if user has access to full features"""
    
    if user.get('is_developer'):
        return True
    
    if role == "buyer":
        return True
    
    if role == "seller":
        now = datetime.now()
        
        if user.get('trial_expires'):
            trial_end = datetime.fromisoformat(user['trial_expires'])
            if now < trial_end:
                return True
        
        if user.get('subscription') == 'paid':
            expires = datetime.fromisoformat(user['subscription_expires'])
            if now < expires:
                return True
            
            if user.get('lifetime_purchased'):
                return True
    
    return False

def count_saved_calculations(email: str) -> int:
    """Count user's saved calculations"""
    calcs = db.reference(f'calculations/{email}').get()
    return len(calcs) if calcs else 0

# ============ PROMO CODE SYSTEM ============

def validate_promo_code(code: str, user_email: str) -> Optional[Dict]:
    """Validate and return promo code details"""
    promo = db.reference(f'promo_codes/{code}').get()
    
    if not promo:
        return None
    
    if promo.get('active') == False:
        return None
    
    if promo.get('expires_at'):
        expires = datetime.fromisoformat(promo['expires_at'])
        if datetime.now() > expires:
            return None
    
    if promo.get('max_uses'):
        uses = promo.get('uses', 0)
        if uses >= promo['max_uses']:
            return None
    
    used_by = promo.get('used_by', [])
    if user_email in used_by:
        return None
    
    return promo

def apply_promo_code(code: str, user_email: str, amount: float) -> Dict:
    """Apply promo code to purchase"""
    promo = validate_promo_code(code, user_email)
    
    if not promo:
        raise HTTPException(status_code=400, detail="Invalid promo code")
    
    discount = 0
    
    if promo.get('type') == 'percentage':
        discount = amount * (promo.get('value', 0) / 100)
    elif promo.get('type') == 'fixed':
        discount = promo.get('value', 0)
    
    new_amount = max(0, amount - discount)
    
    used_by = promo.get('used_by', [])
    used_by.append(user_email)
    db.reference(f'promo_codes/{code}/used_by').set(used_by)
    
    uses = promo.get('uses', 0)
    db.reference(f'promo_codes/{code}/uses').set(uses + 1)
    
    return {
        "original_amount": amount,
        "discount": round(discount, 2),
        "final_amount": round(new_amount, 2),
        "promo_code": code
    }

# ============ STRIPE INTEGRATION ============

@app.post("/api/create-payment-intent")
async def create_payment_intent(
    email: str,
    plan: str,
    tier: str,
    promo_code: Optional[str] = None
):
    """Create Stripe payment intent"""
    
    user = get_user(email)
    
    if not PRICING.get(tier):
        raise HTTPException(status_code=400, detail="Invalid tier")
    
    if not PRICING[tier].get(plan):
        raise HTTPException(status_code=400, detail="Invalid plan")
    
    amount = PRICING[tier][plan]
    
    if promo_code:
        promo_result = apply_promo_code(promo_code, email, amount)
        amount = promo_result['final_amount']
    
    stripe_customer_id = user.get('stripe_customer_id')
    if not stripe_customer_id:
        customer = stripe.Customer.create(email=email)
        stripe_customer_id = customer.id
        db.reference(f'users/{email}/stripe_customer_id').set(stripe_customer_id)
    
    intent = stripe.PaymentIntent.create(
        amount=int(amount * 100),
        currency="usd",
        customer=stripe_customer_id,
        metadata={
            "email": email,
            "tier": tier,
            "plan": plan,
            "user_role": user['role']
        }
    )
    
    return {
        "client_secret": intent.client_secret,
        "amount": amount,
        "currency": "usd"
    }

@app.post("/api/confirm-payment")
async def confirm_payment(
    email: str,
    payment_intent_id: str,
    tier: str,
    plan: str
):
    """Confirm payment and activate subscription"""
    
    user = get_user(email)
    
    intent = stripe.PaymentIntent.retrieve(payment_intent_id)
    
    if intent.status != "succeeded":
        raise HTTPException(status_code=400, detail="Payment not successful")
    
    now = datetime.now()
    if plan == "monthly":
        expires = now + timedelta(days=30)
    elif plan == "annual":
        expires = now + timedelta(days=365)
    elif plan == "lifetime":
        expires = now + timedelta(days=36500)
    
    db.reference(f'users/{email}').update({
        "subscription": "paid",
        "subscription_plan": plan,
        "subscription_expires": expires.isoformat(),
        "subscription_tier": tier,
        "lifetime_purchased": plan == "lifetime"
    })
    
    return {
        "status": "success",
        "subscription_active": True,
        "expires": expires.isoformat()
    }

# ============ CALCULATIONS ============

def calculate_loan(vehicle_price, down_payment, trade_in_value, 
                   trade_in_payoff, loan_term, interest_rate,
                   tax_rate, luxury_tax_rate, fees):
    """Calculate loan payment (CRITICAL: tax only on difference)"""
    
    taxable_amount = vehicle_price - trade_in_value
    sales_tax = taxable_amount * (tax_rate / 100)
    
    luxury_tax = 0
    if luxury_tax_rate > 0:
        luxury_tax = vehicle_price * (luxury_tax_rate / 100)
    
    total_taxes = sales_tax + luxury_tax
    total_fees = sum([fee['amount'] for fee in fees if fee['enabled']])
    
    negative_equity = max(0, trade_in_payoff - trade_in_value)
    
    loan_amount = (vehicle_price + total_taxes + total_fees 
                   - down_payment - trade_in_value + negative_equity)
    
    monthly_rate = interest_rate / 100 / 12
    
    if monthly_rate > 0:
        monthly_payment = (loan_amount * 
                          (monthly_rate * (1 + monthly_rate)**loan_term) / 
                          ((1 + monthly_rate)**loan_term - 1))
    else:
        monthly_payment = loan_amount / loan_term
    
    biweekly_payment = (monthly_payment * 12) / 26
    total_interest = (monthly_payment * loan_term) - loan_amount
    total_cost = vehicle_price + total_taxes + total_fees + total_interest
    
    return {
        "monthly_payment": round(monthly_payment, 2),
        "biweekly_payment": round(biweekly_payment, 2),
        "total_interest": round(total_interest, 2),
        "total_taxes": round(total_taxes, 2),
        "total_fees": round(total_fees, 2),
        "loan_amount": round(loan_amount, 2),
        "total_cost": round(total_cost, 2),
        "negative_equity": round(negative_equity, 2)
    }

@app.post("/api/calculate")
async def api_calculate(email: str, data: dict):
    """Calculate loan payment"""
    try:
        user = get_or_create_user(email, data.get('user_role', 'buyer'))
        
        result = calculate_loan(
            vehicle_price=data.get('vehicle_price', 0),
            down_payment=data.get('down_payment', 0),
            trade_in_value=data.get('trade_in_value', 0),
            trade_in_payoff=data.get('trade_in_payoff', 0),
            loan_term=data.get('loan_term', 60),
            interest_rate=data.get('interest_rate', 0),
            tax_rate=data.get('tax_rate', 0),
            luxury_tax_rate=data.get('luxury_tax_rate', 0),
            fees=data.get('fees', [])
        )
        
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============ SAVED CALCULATIONS ============

@app.post("/api/calculations/save")
async def save_calculation(email: str, data: dict):
    """Save calculation"""
    try:
        user = get_user(email)
        
        if user['role'] == 'buyer' and user.get('subscription') == 'free':
            saved_count = count_saved_calculations(email)
            if saved_count >= FREE_TIER_SAVES:
                raise HTTPException(
                    status_code=403, 
                    detail=f"Free tier limited to {FREE_TIER_SAVES} saves. Upgrade to save more."
                )
        
        calc_id = db.reference(f'calculations/{email}').push({
            'name': data.get('name'),
            'data': data.get('calculation_data'),
            'created_at': datetime.now().isoformat()
        }).key
        
        return {"calc_id": calc_id, "message": "Saved!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/calculations/{email}")
async def get_calculations(email: str):
    """Get user's saved calculations"""
    try:
        user = get_user(email)
        calcs = db.reference(f'calculations/{email}').get()
        return calcs or {}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============ FEEDBACK ============

@app.post("/api/feedback")
async def save_feedback(email: str, feedback: dict):
    """Save user feedback"""
    try:
        feedback_id = db.reference('feedback').push({
            'email': email,
            'type': feedback.get('type'),
            'message': feedback.get('message'),
            'created_at': datetime.now().isoformat(),
            'status': 'new'
        }).key
        
        return {"feedback_id": feedback_id, "message": "Thank you!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ============ ADMIN - PROMO CODES ============

@app.post("/api/admin/promo-codes/create")
async def create_promo_code(admin_email: str, promo: dict):
    """Create new promo code (Admin only)"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    code = promo.get('code').upper()
    
    promo_data = {
        'code': code,
        'type': promo.get('type'),
        'value': promo.get('value'),
        'active': True,
        'created_at': datetime.now().isoformat(),
        'expires_at': promo.get('expires_at'),
        'max_uses': promo.get('max_uses'),
        'uses': 0,
        'used_by': []
    }
    
    db.reference(f'promo_codes/{code}').set(promo_data)
    
    return {"code": code, "message": "Promo code created"}

@app.get("/api/admin/promo-codes")
async def list_promo_codes(admin_email: str):
    """List all promo codes (Admin only)"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    codes = db.reference('promo_codes').get()
    return codes or {}

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
