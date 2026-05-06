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
# ============ ADMIN - POP-UP NOTIFICATIONS ============

@app.post("/api/admin/pop-ups/create")
async def create_pop_up(admin_email: str, pop_up: dict):
    """Create new pop-up campaign targeting specific user groups"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    pop_up_id = pop_up.get('title').lower().replace(' ', '_') + '_' + datetime.now().strftime('%Y%m%d%H%M%S')
    
    pop_up_data = {
        'id': pop_up_id,
        'title': pop_up.get('title'),
        'text': pop_up.get('text'),
        'button_1': pop_up.get('button_1'),
        'button_2': pop_up.get('button_2'),
        'target_groups': pop_up.get('target_groups', []),
        'start_date': pop_up.get('start_date'),
        'end_date': pop_up.get('end_date'),
        'linked_promo_code': pop_up.get('linked_promo_code'),
        'status': 'active',
        'created_at': datetime.now().isoformat(),
        'impressions': 0,
        'clicks': 0,
        'revenue': 0,
        'target_count': 0
    }
    
    db.reference(f'pop_ups/{pop_up_id}').set(pop_up_data)
    
    return {
        "pop_up_id": pop_up_id,
        "message": "Campaign created",
        "target_groups": pop_up.get('target_groups')
    }

@app.get("/api/pop-ups")
async def get_active_pop_ups(user_email: str):
    """Get active pop-ups for user based on their group"""
    
    try:
        user = get_user(user_email)
        user_groups = determine_user_groups(user)
        
        now = datetime.now()
        pop_ups = db.reference('pop_ups').get()
        
        active_pop_ups = []
        
        if pop_ups:
            for pop_up_id, pop_up_data in pop_ups.items():
                if pop_up_data.get('status') == 'active':
                    start = datetime.fromisoformat(pop_up_data['start_date'])
                    end = datetime.fromisoformat(pop_up_data['end_date'])
                    
                    if start <= now <= end:
                        # Проверяем совпадает ли группа юзера с целевыми группами
                        target_groups = pop_up_data.get('target_groups', [])
                        
                        if any(group in target_groups for group in user_groups):
                            active_pop_ups.append({
                                'id': pop_up_data['id'],
                                'title': pop_up_data['title'],
                                'text': pop_up_data['text'],
                                'button_1': pop_up_data['button_1'],
                                'button_2': pop_up_data['button_2'],
                                'linked_promo_code': pop_up_data.get('linked_promo_code')
                            })
        
        return {"pop_ups": active_pop_ups}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/pop-ups/click")
async def track_pop_up_click(user_email: str, pop_up_id: str, button_clicked: str, promo_code_applied: Optional[str] = None):
    """Track when user clicks pop-up button"""
    
    try:
        # Сохраняем что юзер нажимал
        interaction_data = {
            'clicked_at': datetime.now().isoformat(),
            'button': button_clicked
        }
        
        if promo_code_applied:
            interaction_data['promo_code_applied'] = promo_code_applied
        
        db.reference(f'pop_up_interactions/{user_email}/{pop_up_id}').set(interaction_data)
        
        # Увеличиваем счётчик clicks
        pop_up_ref = db.reference(f'pop_ups/{pop_up_id}')
        pop_up_data = pop_up_ref.get()
        
        if pop_up_data:
            new_clicks = (pop_up_data.get('clicks', 0) or 0) + 1
            pop_up_ref.update({'clicks': new_clicks})
        
        return {"status": "tracked", "pop_up_id": pop_up_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/pop-ups/impression")
async def track_pop_up_impression(user_email: str, pop_up_id: str):
    """Track when pop-up is shown to user"""
    
    try:
        # Сохраняем что юзер видел pop-up
        db.reference(f'pop_up_interactions/{user_email}/{pop_up_id}').set({
            'viewed_at': datetime.now().isoformat()
        }, merge=True)
        
        # Увеличиваем счётчик impressions
        pop_up_ref = db.reference(f'pop_ups/{pop_up_id}')
        pop_up_data = pop_up_ref.get()
        
        if pop_up_data:
            new_impressions = (pop_up_data.get('impressions', 0) or 0) + 1
            pop_up_ref.update({'impressions': new_impressions})
        
        return {"status": "tracked"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/admin/pop-ups/stats")
async def get_pop_up_stats(admin_email: str):
    """Get statistics for all pop-up campaigns"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    pop_ups = db.reference('pop_ups').get()
    
    stats = []
    if pop_ups:
        for pop_up_id, pop_up_data in pop_ups.items():
            impressions = pop_up_data.get('impressions', 0) or 0
            clicks = pop_up_data.get('clicks', 0) or 0
            ctr = round((clicks / impressions * 100), 1) if impressions > 0 else 0
            
            stats.append({
                'id': pop_up_id,
                'title': pop_up_data['title'],
                'status': pop_up_data['status'],
                'target_groups': pop_up_data.get('target_groups', []),
                'impressions': impressions,
                'clicks': clicks,
                'ctr': f"{ctr}%",
                'revenue': pop_up_data.get('revenue', 0),
                'start_date': pop_up_data['start_date'],
                'end_date': pop_up_data['end_date'],
                'linked_promo': pop_up_data.get('linked_promo_code')
            })
    
    return {"pop_ups": stats}

def determine_user_groups(user: Dict) -> list:
    """Determine which groups user belongs to (can be multiple)"""
    
    groups = []
    now = datetime.now()
    
    # Основная группа (одна из них)
    if user.get('subscription') == 'free':
        groups.append('free_users')
    elif user.get('subscription') == 'paid':
        groups.append('paid_users')
    
    # Дополнительные группы
    if user.get('lifetime_purchased'):
        groups.append('lifetime_users')
    
    if user.get('trial_expires'):
        trial_end = datetime.fromisoformat(user['trial_expires'])
        if now < trial_end:
            groups.append('trial_users')
        elif (now - trial_end).days >= 0 and (now - trial_end).days <= 30:
            groups.append('expired_trial')
    
    if user.get('subscription_expires'):
        sub_end = datetime.fromisoformat(user['subscription_expires'])
        if now > sub_end and (now - sub_end).days <= 90:
            groups.append('expired_subscription')
    
    if user.get('last_login'):
        last_login = datetime.fromisoformat(user['last_login'])
        if (now - last_login).days > 30:
            groups.append('inactive_users')
    
    groups.append('all_users')
    
    return groups

@app.post("/api/admin/pop-ups/{pop_up_id}/pause")
async def pause_campaign(admin_email: str, pop_up_id: str):
    """Pause a running campaign"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    db.reference(f'pop_ups/{pop_up_id}').update({'status': 'paused'})
    
    return {"status": "paused", "pop_up_id": pop_up_id}

@app.post("/api/admin/pop-ups/{pop_up_id}/end")
async def end_campaign(admin_email: str, pop_up_id: str):
    """End a campaign permanently"""
    
    if admin_email.lower() != DEVELOPER_EMAIL.lower():
        raise HTTPException(status_code=403, detail="Admin access required")
    
    db.reference(f'pop_ups/{pop_up_id}').update({'status': 'ended'})
    
    return {"status": "ended", "pop_up_id": pop_up_id}

# ============ HEALTH CHECK ============

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
