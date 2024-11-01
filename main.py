import os
import crud
import models
import schemas
import stripe_crud
import resend_crud
import messages
import scheduler
from datetime import date
from fastapi import Depends, FastAPI, HTTPException, Request, status, Query
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from typing import List
from database import SessionLocal, engine
import logging
import time
from stripe_main import router as stripe_router
from twilio.base.exceptions import TwilioRestException

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

models.Base.metadata.create_all(bind=engine)

app = FastAPI(
    root_path="/api",
    open_api_url="/api/openapi.json",
)

scheduler_obj = scheduler.setup_scheduler()

API_SECRET_KEY = os.getenv("API_SECRET_KEY")
BASE_URL = os.getenv("BASE_URL")

class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/stripe-webhook":
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            raise HTTPException(status_code=403, detail="Invalid or missing API Key")

        api_key = auth_header.split("Bearer ")[1]
        if api_key != API_SECRET_KEY:
            raise HTTPException(status_code=403, detail="Invalid API Key")

        response = await call_next(request)
        return response

if BASE_URL == "https://resera.com.au":
    app.add_middleware(APIKeyMiddleware)

app.include_router(stripe_router)

class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start_time = time.time()
        response = await call_next(request)
        process_time = time.time() - start_time
        logger.info(f"{request.method} {request.url.path} - Status: {response.status_code} - Duration: {process_time:.2f}s")
        return response

app.add_middleware(RequestLoggingMiddleware)

allowed_origin = os.getenv("BASE_URL")

if allowed_origin != "https://resera.com.au":
    app.docs_url = "/docs"
    app.redoc_url = "/redoc"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[allowed_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    exc_str = f'{exc}'.replace('  ', ' ')
    logger.error(f'Validation error: {exc_str}')
    content = {'status_code': 422, 'message': exc_str, 'data': None, 'errors': exc.errors()}
    return JSONResponse(content=content, status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.on_event("startup")
def start_scheduler():
    scheduler_obj.start()

@app.on_event("shutdown")
def shutdown_scheduler():
    scheduler_obj.shutdown()

@app.post("/check-payments", status_code=status.HTTP_202_ACCEPTED, tags=["Payment-Checking"])
async def trigger_payment_check():
    scheduler.check_payments_and_send_reminders()
    return {"message": "Payment check initiated"}

# Landlord routes
@app.get("/landlords", response_model=List[schemas.Landlord], status_code=status.HTTP_200_OK, tags=["Landlord"])
def read_landlords(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    logger.info(f"Reading landlords with skip: {skip} and limit: {limit}")
    landlords = crud.get_landlords(db, skip=skip, limit=limit)
    return landlords

@app.get("/landlord/{landlord_id}", response_model=schemas.Landlord, status_code=status.HTTP_200_OK, tags=["Landlord"])
def read_landlord(landlord_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching landlord with id: {landlord_id}")
    landlord = crud.get_landlord(db, landlord_id)
    if landlord is None:
        logger.error(f"Landlord with id {landlord_id} not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Landlord not found")
    return landlord

@app.put("/landlord/{landlord_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Landlord"])
def update_landlord(landlord_id: int, landlord: schemas.LandlordUpdate, db: Session = Depends(get_db)):
    logger.info(f"Updating landlord with id: {landlord_id}")
    try:
        updated_landlord = crud.update_landlord(db, landlord_id, landlord)
        logger.info(f"Successfully updated lease with id: {landlord_id}")
        return update_landlord
    except HTTPException as e:
        logger.error(f"Error updating landlord: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating landlord: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/landlord/email/{email_address}", response_model=schemas.Landlord, status_code=status.HTTP_200_OK, tags=["Landlord"])
def read_landlord_by_email(email_address: str, db: Session = Depends(get_db)):
    logger.info(f"Fetching landlord with email: {email_address}")
    landlord = crud.get_landlord_by_email(db, email_address)
    if landlord is None:
        logger.error(f"Landlord with email {email_address} not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Landlord not found")
    return landlord

@app.post("/landlord", response_model=schemas.Landlord, status_code=status.HTTP_201_CREATED, tags=["Landlord"])
def create_landlord(landlord: schemas.LandlordCreate, db: Session = Depends(get_db)):
    logger.info(f"Creating new landlord: {landlord.dict()}")
    try:
        db_landlord = crud.create_landlord(db, landlord)
        logger.info(f"Successfully created landlord with id: {db_landlord.id}")
        return db_landlord
    except Exception as e:
        logger.error(f"Error creating landlord: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.delete("/landlord/{landlord_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Landlord"])
def delete_landlord(landlord_id: int, db: Session = Depends(get_db)):
    logger.info(f"Deleting landlord with id: {landlord_id}")
    try:
        crud.delete_landlord(db, landlord_id=landlord_id)
        logger.info(f"Successfully deleted landlord with id: {landlord_id}")
    except HTTPException as e:
        logger.error(f"Error deleting landlord: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error deleting landlord: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Property routes
@app.get("/properties", response_model=List[schemas.Property], status_code=status.HTTP_200_OK, tags=["Property"])
def get_properties(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    logger.info(f"Fetching properties with skip={skip} and limit={limit}")
    try:
        properties = crud.get_properties(db, skip=skip, limit=limit)
        logger.info(f"Successfully fetched {len(properties)} properties")
        return properties
    except Exception as e:
        logger.error(f"Error fetching properties: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/property/{property_id}", response_model=schemas.Property, status_code=status.HTTP_200_OK, tags=["Property"])
def read_property(property_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching property with id: {property_id}")
    db_property = crud.get_property(db, property_id=property_id)
    if db_property is None:
        logger.warning(f"Property not found: {property_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
    logger.info(f"Successfully fetched property: {db_property.id}")
    return db_property

@app.get("/landlord/{landlord_id}/properties", response_model=List[schemas.Property], status_code=status.HTTP_200_OK, tags=["Property"])
def get_landlord_properties(landlord_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching properties for landlord with id: {landlord_id}")
    properties = crud.get_property_by_landlord(db, landlord_id=landlord_id)
    logger.info(f"Successfully fetched {len(properties)} properties")
    return properties

@app.post("/property", response_model=schemas.Property, status_code=status.HTTP_201_CREATED, tags=["Property"])
def create_property(property: schemas.PropertyCreate, db: Session = Depends(get_db)):
    logger.info(f"Creating new property: {property.dict()}")
    try:
        new_property = crud.create_property(db=db, property=property)
        logger.info(f"Successfully created property with id: {new_property.id}")
        return new_property
    except Exception as e:
        logger.error(f"Error creating property: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.put("/property/{property_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Property"])
def update_property(property_id: int, property: schemas.PropertyUpdate, db: Session = Depends(get_db)):
    logger.info(f"Updating property with id: {property_id}")
    try:
        updated_property = crud.update_property(db, property_id, property)
        logger.info(f"Successfully updated property with id: {property_id}")
        return updated_property
    except HTTPException as e:
        logger.error(f"Error updating property: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating property: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.delete("/property/{property_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Property"])
def delete_property(property_id: int, db: Session = Depends(get_db)):
    logger.info(f"Deleting property with id: {property_id}")
    try:
        crud.delete_property(db, property_id=property_id)
        logger.info(f"Successfully deleted property: {property_id}")
    except HTTPException as e:
        logger.warning(f"Property not found: {property_id}")
        raise e
    except Exception as e:

        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/property/{property_id}/payments", response_model=List[schemas.Payment], status_code=status.HTTP_200_OK, tags=["Payment"])
def read_property_payments(property_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching payments for property with id: {property_id}")
    try:
        property = crud.get_property(db, property_id=property_id)
        if property is None:
            logger.warning(f"Property not found: {property_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")

        payments = crud.get_property_payments(db, property_id)
        logger.info(f"Successfully fetched {len(payments)} payments for property: {property_id}")
        return payments
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching payments for property {property_id}: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Tenant routes
@app.get("/tenants", response_model=List[schemas.Tenant], status_code=status.HTTP_200_OK, tags=["Tenant"])
def read_tenants(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    logger.info(f"Fetching tenants with skip={skip} and limit={limit}")
    try:
        tenants = crud.get_tenants(db, skip=skip, limit=limit)
        logger.info(f"Successfully fetched {len(tenants)} tenants")
        return tenants
    except Exception as e:
        logger.error(f"Error fetching tenants: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/tenant/{tenant_id}", response_model=schemas.Tenant, status_code=status.HTTP_200_OK, tags=["Tenant"])
def read_tenant(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching tenant with id: {tenant_id}")
    db_tenant = crud.get_tenant(db, tenant_id=tenant_id)
    if db_tenant is None:
        logger.warning(f"Tenant not found: {tenant_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    logger.info(f"Successfully fetched tenant: {db_tenant.id}")
    return db_tenant

@app.post("/tenant", response_model=schemas.Tenant, status_code=status.HTTP_201_CREATED, tags=["Tenant"])
def create_tenant_for_property(tenant: schemas.TenantCreate, db: Session = Depends(get_db)):
    logger.info(f"Creating new tenant for property {tenant.property_id}: {tenant.dict()}")
    try:
        new_tenant = crud.create_property_tenant(db=db, tenant=tenant)
        logger.info(f"Successfully created tenant with id: {new_tenant.id}")
        return new_tenant
    except Exception as e:
        logger.error(f"Error creating tenant: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.put("/tenant/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Tenant"])
def update_tenant(tenant_id: int, tenant: schemas.TenantUpdate, db: Session = Depends(get_db)):
    logger.info(f"Updating tenant with id: {tenant_id}")
    try:
        updated_tenant = crud.update_tenant(db, tenant_id, tenant)
        logger.info(f"Successfully updated tenant with id: {tenant_id}")
        return updated_tenant
    except HTTPException as e:
        logger.error(f"Error updating tenant: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating tenant: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.delete("/tenant/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Tenant"])
def delete_tenant(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Deleting tenant with id: {tenant_id}")
    try:
        crud.delete_tenant(db, tenant_id=tenant_id)
        logger.info(f"Successfully deleted tenant: {tenant_id}")
    except HTTPException as e:
        logger.warning(f"Tenant not found: {tenant_id}")
        raise e
    except Exception as e:
        logger.error(f"Error deleting tenant: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Lease routes
@app.get("/leases", response_model=List[schemas.Lease], status_code=status.HTTP_200_OK, tags=["Lease"])
def get_leases(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    logger.info(f"Fetching leases with skip={skip} and limit={limit}")
    try:
        leases = crud.get_leases(db, skip=skip, limit=limit)
        logger.info(f"Successfully fetched {len(leases)} leases")
        return leases
    except Exception as e:
        logger.error(f"Error fetching leases: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/lease/{lease_id}", response_model=schemas.Lease, status_code=status.HTTP_200_OK, tags=["Lease"])
def read_lease(lease_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching lease with id: {lease_id}")
    lease = crud.get_lease(db, lease_id=lease_id)
    if lease is None:
        logger.warning(f"Lease not found: {lease_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
    logger.info(f"Successfully fetched lease: {lease.id}")
    return lease

@app.post("/lease", response_model=schemas.Lease, status_code=status.HTTP_201_CREATED, tags=["Lease"])
def create_lease_route(
    lease: schemas.LeaseCreate, 
    sendWelcome: bool = Query(False, description="Send welcome email and SMS to tenant"),
    db: Session = Depends(get_db)
):
    logger.info(f"Creating new lease: {lease.dict()}, sendWelcome: {sendWelcome}")
    try:
        new_lease = crud.create_lease(db=db, lease=lease, send_welcome=sendWelcome)
        if new_lease:
            logger.info(f"Successfully created lease with id: {new_lease.id}")
            return new_lease
        else:
            logger.error("Lease creation failed without raising an exception")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Lease creation failed")
    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"Unhandled error in create_lease_route: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred")
    
@app.put("/lease/{lease_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Lease"])
def update_lease(lease_id: int, lease: schemas.LeaseUpdate, db: Session = Depends(get_db)):
    logger.info(f"Updating lease with id: {lease_id}")
    try:
        updated_lease = crud.update_lease(db, lease_id, lease)
        logger.info(f"Successfully updated lease with id: {lease_id}")
        return updated_lease
    except HTTPException as e:
        logger.error(f"Error updating lease: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating lease: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/tenant/{tenant_id}/lease", response_model=schemas.Lease, status_code=status.HTTP_200_OK, tags=["Lease"])
def get_tenant_lease(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching lease for tenant with id: {tenant_id}")
    lease = crud.get_tenant_lease(db, tenant_id=tenant_id)
    if lease is None:
        logger.warning(f"Lease not found for tenant: {tenant_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
    logger.info(f"Successfully fetched lease: {lease.id}")
    return lease

# Payment routes
@app.get("/payments", response_model=List[schemas.Payment], status_code=status.HTTP_200_OK, tags=["Payment"])
def get_payments(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    logger.info(f"Fetching payments with skip={skip} and limit={limit}")
    payments = crud.get_payments(db, skip=skip, limit=limit)
    logger.info(f"Successfully fetched {len(payments)} payments")
    return payments

@app.get("/payment/{payment_id}", response_model=schemas.Payment, status_code=status.HTTP_200_OK, tags=["Payment"])
def get_payment(payment_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching payment with id: {payment_id}")
    payment = crud.get_payment(db, payment_id=payment_id)
    if payment is None:
        logger.warning(f"Payment not found: {payment_id}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    logger.info(f"Successfully fetched payment: {payment.id}")
    return payment

@app.get("/lease/{lease_id}/payments", response_model=List[schemas.Payment], status_code=status.HTTP_200_OK, tags=["Payment"])
def get_lease_payments(lease_id: int, db: Session = Depends(get_db)):
    logger.info(f"Fetching payments for lease with id: {lease_id}")
    payments = crud.get_lease_payments(db, lease_id)
    logger.info(f"Successfully fetched {len(payments)} payments")
    return payments

@app.post("/payment", response_model=schemas.Payment, status_code=status.HTTP_201_CREATED, tags=["Payment"])
def create_payment(payment: schemas.PaymentCreate, db: Session = Depends(get_db)):
    logger.info(f"Creating new payment: {payment.dict()}")
    try:
        new_payment = crud.create_payment(db=db, payment=payment)
        logger.info(f"Successfully created payment with id: {new_payment.id}")
        return new_payment
    except Exception as e:
        logger.error(f"Error creating payment: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.put("/payment/{payment_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Payment"])
def update_payment(payment_id: int, payment: schemas.PaymentUpdate, db: Session = Depends(get_db)):
    logger.info(f"Updating payment with id: {payment_id}")
    try:
        updated_payment = crud.update_payment(db, payment_id, payment)
        logger.info(f"Successfully updated payment with id: {payment_id}")
    except HTTPException as e:
        logger.error(f"Error updating payment: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error updating payment: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.delete("/payment/{payment_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Payment"])
def delete_payment(payment_id: int, db: Session = Depends(get_db)):
    logger.info(f"Deleting payment with id: {payment_id}")
    try:
        crud.delete_payment(db, payment_id=payment_id)
        logger.info(f"Successfully deleted payment: {payment_id}")
    except HTTPException as e:
        logger.warning(f"Payment not found: {payment_id}")
        raise e
    except Exception as e:
        logger.error(f"Error deleting payment: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Stripe Account routes
@app.post("/account/{landlord_id}", status_code=status.HTTP_201_CREATED, tags=["Stripe Account"])
def create_account(landlord_id: int, db: Session = Depends(get_db)):
    logger.info(f"Creating account for landlord with id: {landlord_id}")
    try:
        landlord = crud.get_landlord(db, landlord_id=landlord_id)
        account = stripe_crud.create_stripe_account(landlord)
        logger.info(f"Successfully created account for landlord: {landlord_id}")
        return account
    except Exception as e:
        logger.error(f"Error creating account: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/account/{account_id}", status_code=status.HTTP_200_OK, tags=["Stripe Account"])
def get_account(account_id: str):
    logger.info(f"Fetching account with id: {account_id}")
    try:
        account = stripe_crud.get_stripe_account(account_id)
        logger.info(f"Successfully fetched account: {account_id}")
        return account
    except Exception as e:
        logger.error(f"Error fetching account: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.post("/account/{account_id}/account-link", status_code=status.HTTP_201_CREATED, tags=["Stripe Account"])
def create_account_link(account_id: str):
    logger.info(f"Creating account link for account with id: {account_id}")
    try:
        link = stripe_crud.create_account_link(account_id)
        logger.info(f"Successfully created account link for account: {account_id}")
        return link
    except HTTPException as e:
        logger.error(f"Error creating account link: {str(e)}")
        raise e
    except Exception as e:
        logger.error(f"Unexpected error creating account link: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.post("/account/{account_id}/session", status_code=status.HTTP_201_CREATED, tags=["Stripe Account"])
def create_account_session(account_id: str):
    logger.info(f"Creating account session for account: {account_id}")
    try:
        session = stripe_crud.create_account_session(account_id)
        logger.info(f"Successfully created account session for account: {account_id}")
        return session
    except Exception as e:
        logger.error(f"Error creating account session: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.post("/account/{account_id}/login-link", status_code=status.HTTP_201_CREATED, tags=["Stripe Account"])
def create_login_link(account_id: str):
    logger.info(f"Creating login link for account: {account_id}")
    try:
        session = stripe_crud.create_login_link(account_id)
        logger.info(f"Successfully created login link for account: {account_id}")
        return session
    except Exception as e:
        logger.error(f"Error creating account session: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Checkout Session routes
@app.post("/checkout/{lease_id}/{payment_id}", status_code=status.HTTP_201_CREATED, tags=["Checkout Session"])
def create_checkout_session(lease_id: int, payment_id: int, db: Session = Depends(get_db)):
    logger.info(f"Creating checkout session for lease: {lease_id} and payment: {payment_id}")
    try:
        session = stripe_crud.create_checkout_session(db, lease_id, payment_id)
        logger.info(f"Successfully created checkout session for lease: {lease_id} and payment: {payment_id}")
        return session
    except Exception as e:
        logger.error(f"Error creating checkout session: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/checkout/{checkout_id}", status_code=status.HTTP_200_OK, tags=["Checkout Session"])
def get_checkout_session(checkout_id: str):
    logger.info(f"Fetching checkout session with id: {checkout_id}")
    try:
        session = stripe_crud.get_checkout_session(checkout_id)
        logger.info(f"Successfully fetched checkout session: {checkout_id}")
        return session
    except Exception as e:
        logger.error(f"Error fetching checkout session: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Product routes
@app.post("/product", status_code=status.HTTP_201_CREATED, tags=["Product"])
def create_product(lease_id: int, payment_id: int, tenant_name: str):
    logger.info(f"Creating product for lease: {lease_id} and payment: {payment_id}")
    try:
        product = stripe_crud.create_product(lease_id, payment_id, tenant_name, True)
        logger.info(f"Successfully created product for lease: {lease_id} and payment: {payment_id}")
        return product
    except Exception as e:
        logger.error(f"Error creating product: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/products", status_code=status.HTTP_200_OK, tags=["Product"])
def get_products(limit: int = 10):
    logger.info("Fetching all products")
    try:
        products = stripe_crud.get_products()
        logger.info("Successfully fetched all products")
        return products
    except Exception as e:
        logger.error(f"Error fetching products: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/product/{lease_id}/{payment_id}", status_code=status.HTTP_200_OK, tags=["Product"])
def get_product(lease_id: int, payment_id: int):
    logger.info(f"Fetching product with id: {lease_id} and {payment_id}")
    try:
        product = stripe_crud.get_product(lease_id, payment_id)
        if not product:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
        logger.info(f"Successfully fetched product with id: {lease_id} and {payment_id}")
        return product
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching product: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Price routes
@app.get("/prices", status_code=status.HTTP_200_OK, tags=["Price"])
def get_prices(limit: int = 10):
    logger.info("Fetching all prices")
    try:
        prices = stripe_crud.get_prices()
        logger.info("Successfully fetched all prices")
        return prices
    except Exception as e:
        logger.error(f"Error fetching prices: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.post("/price/{product_id}/{amount}", status_code=status.HTTP_201_CREATED, tags=["Price"])
def create_price(product_id: str, amount: int):
    logger.info(f"Creating price for product: {product_id} and amount: {amount}")
    try:
        price = stripe_crud.create_price(product_id, amount)
        logger.info(f"Successfully created price for product: {product_id} and amount: {amount}")
        return price
    except Exception as e:
        logger.error(f"Error creating price: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Account Information
@app.get("/account/{account_id}", status_code=status.HTTP_200_OK, tags=["Account"])
def get_connect_account(account_id: str):
    logger.info(f"Retrieving account: {account_id}")
    try:
        account = stripe_crud.get_stripe_account(account_id)
        logger.info(f"Successfully retrieved account: {account_id}")
        return account
    except Exception as e:
        logger.error(f"Error retrieving account: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/balance/{account_id}", status_code=status.HTTP_200_OK, tags=["Account"])
def get_balance(account_id: str):
    logger.info(f"Retrieving balance for account: {account_id}")
    try:
        balance = stripe_crud.retrieve_account_balance(account_id)
        logger.info(f"Successfully retrieved balance for account: {account_id}")
        return balance
    except Exception as e:
        logger.error(f"Error retrieving balance: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/payouts/{account_id}", status_code=status.HTTP_200_OK, tags=["Account"])
def get_payouts(account_id: str):
    logger.info(f"Retrieving payouts for account: {account_id}")
    try:
        payouts = stripe_crud.retrieve_account_payouts(account_id)
        logger.info(f"Successfully retrieved payouts for account: {account_id}")
        return payouts.data
    except Exception as e:
        logger.error(f"Error retrieving payouts: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/balance-transactions/{account_id}", status_code=status.HTTP_200_OK, tags=["Account"])
def get_balance_transaction(account_id: str):
    logger.info(f"Retrieving balance transaction for account: {account_id}")
    try:
        balance_transactions = stripe_crud.retrieve_account_balance_transaction(account_id)
        logger.info(f"Successfully retrieved balance transaction for account: {account_id}")
        return balance_transactions.data
    except Exception as e:
        logger.error(f"Error retrieving balance transactions: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.get("/charge/{account_id}", status_code=status.HTTP_200_OK, tags=["Account"])
def get_charges(account_id: str):
    logger.info(f"Retrieving charges for account: {account_id}")
    try:
        charges = stripe_crud.retrieve_account_charges(account_id)
        logger.info(f"Successfully retrieved charge for account: {account_id}")
        return charges.data
    except Exception as e:
        logger.error(f"Error retrieving charges: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

@app.post("/send-payment-link-email/{tenant_id}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.EmailResponse, tags=["Email"])
async def send_payment_link(tenant_id: int, db: Session = Depends(get_db)):
    try:
        tenant = crud.get_tenant(db, tenant_id)
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        lease = crud.get_tenant_lease(db, tenant_id)
        if not lease:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")

        email_id = resend_crud.send_payment_link_email(tenant, lease)
        return schemas.EmailResponse(success=True, message="Email sent successfully", email_id=email_id)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error in send_payment_link: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred")

@app.post("/send-reminder-email/{tenant_id}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.EmailResponse, tags=["Email"])
async def send_reminder_email(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Received request to send reminder email for tenant_id: {tenant_id}")
    try:
        tenant = crud.get_tenant(db, tenant_id)
        if not tenant:
            logger.warning(f"Tenant not found for tenant_id: {tenant_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        logger.info(f"Retrieved tenant information for tenant_id: {tenant_id}")

        lease = crud.get_tenant_lease(db, tenant_id)
        if not lease:
            logger.warning(f"Lease not found for tenant_id: {tenant_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
        logger.info(f"Retrieved lease information for tenant_id: {tenant_id}")

        property = crud.get_property(db, lease.property_id)
        if not property:
            logger.warning(f"Property not found for property_id: {lease.property_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
        logger.info(f"Retrieved property information for property_id: {lease.property_id}")
        
        if lease.next_payment_date < date.today():
            logger.info(f"Sending overdue payment email for tenant_id: {tenant_id}")
            email_id = resend_crud.send_overdue_payment_email(tenant, lease, property)
        else:
            logger.info(f"Sending payment reminder email for tenant_id: {tenant_id}")
            email_id = resend_crud.send_payment_reminder_email(tenant, lease, property)
        
        logger.info(f"Email sent successfully for tenant_id: {tenant_id}, email_id: {email_id}")
        return schemas.EmailResponse(success=True, message="Email sent successfully", email_id=email_id)
    except ValueError as e:
        logger.error(f"ValueError in send_reminder_email for tenant_id {tenant_id}: {str(e)}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.exception(f"Unexpected error in send_reminder_email for tenant_id {tenant_id}: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred")
        
@app.post("/send-overdue-email/{tenant_id}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.EmailResponse, tags=["Email"])
async def send_overdue_email(tenant_id: int, db: Session = Depends(get_db)):
    try:
        tenant = crud.get_tenant(db, tenant_id)
        if not tenant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

        lease = crud.get_tenant_lease(db, tenant_id)
        if not lease:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")

        property = crud.get_property(db, lease.property_id)
        if not property:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")

        email_id = resend_crud.send_overdue_payment_email(tenant, lease, property)
        return schemas.EmailResponse(success=True, message="Email sent successfully", email_id=email_id)

    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected error in send_payment_link: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred")

@app.post("/demo/{email}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.EmailResponse, tags=["Email"])
async def send_demo_request_email(
    email: str
):
    logger.info(f"Received demo request from email: {email}")
    try:
        email_id = resend_crud.send_demo_email(email)
        logger.info(f"Demo request email sent successfully to {email}, email_id: {email_id}")
        return schemas.EmailResponse(
            success=True, 
            message="Demo request email sent successfully", 
            email_id=email_id
        )
    except ValueError as e:
        logger.error(f"ValueError in send_demo_request_email for {email}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=str(e)
        )
    except Exception as e:
        logger.exception(f"Unexpected error in send_demo_request_email for {email}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="An unexpected error occurred while sending demo request email"
        )
        
        
@app.post("/message/{phone_number}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.MessageResponseModel, tags=["Phone"])
async def send_tenant_message(phone_number: str, db: Session = Depends(get_db)):
    try:
        message = messages.send_message(phone_number, "McDonalds: Hey Khoi, for being a loyal customer to McDonalds you have won free fries for a year! \n Reply to this message to claim reward \n Fun Fact: You have order 136 Soy Lattes this year!")
        return schemas.MessageResponseModel(
            success=True,
            message=f"Message sent successfully. SID: {message.sid}, Message Body: '{message.body}'"
        )
    except ValueError as e:
        return schemas.MessageResponseModel(success=False, message=str(e))
    except Exception as e:
        logger.error(f"Unexpected error in sending tenant message: {str(e)}")
        return schemas.MessageResponseModel(success=False, message="An unexpected error occurred")

@app.post("/message/late/{tenant_id}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.MessageResponseModel, tags=["Phone"])
async def send_late_message_to_tenant(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Received request to send late message for tenant_id: {tenant_id}")
    try:
        tenant = crud.get_tenant(db, tenant_id)
        if not tenant:
            logger.warning(f"Tenant not found for tenant_id: {tenant_id}")
            return schemas.MessageResponseModel(success=False, message=f"Tenant not found for id: {tenant_id}")

        lease = crud.get_tenant_lease(db, tenant_id)
        if not lease:
            logger.warning(f"Lease not found for tenant_id: {tenant_id}")
            return schemas.MessageResponseModel(success=False, message=f"Lease not found for tenant_id: {tenant_id}")

        logger.info(f"Sending late message to tenant: {tenant.name}, phone: {tenant.phone}")
        message = messages.send_late_message(tenant.phone, tenant.name, lease.next_payment_date, lease.payment_link_url)
        logger.info(f"Late message sent successfully to tenant_id: {tenant_id}")
        return schemas.MessageResponseModel(
            success=True,
            message=f"Message sent successfully. SID: {message.sid}, Message Body: '{message.body}'"
        )
    except ValueError as e:
        logger.error(f"ValueError in send_late_message_to_tenant: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=str(e))
    except TwilioRestException as e:
        logger.error(f"TwilioRestException in send_late_message_to_tenant: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=f"Error sending message via Twilio: {str(e)}")
    except Exception as e:
        logger.exception(f"Unexpected error in send_late_message_to_tenant for tenant_id {tenant_id}: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=f"An unexpected error occurred: {str(e)}")

@app.post("/message/reminder/{tenant_id}", status_code=status.HTTP_202_ACCEPTED, response_model=schemas.MessageResponseModel, tags=["Phone"])
async def send_payment_message_reminder(tenant_id: int, db: Session = Depends(get_db)):
    logger.info(f"Received request to send payment reminder message for tenant_id: {tenant_id}")
    try:
        tenant = crud.get_tenant(db, tenant_id)
        if not tenant:
            logger.warning(f"Tenant not found for tenant_id: {tenant_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        logger.info(f"Retrieved tenant information for tenant_id: {tenant_id}")

        lease = crud.get_tenant_lease(db, tenant_id)
        if not lease:
            logger.warning(f"Lease not found for tenant_id: {tenant_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lease not found")
        logger.info(f"Retrieved lease information for tenant_id: {tenant_id}")

        property = crud.get_property(db, lease.property_id)
        if not property:
            logger.warning(f"Property not found for property_id: {lease.property_id}")
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Property not found")
        logger.info(f"Retrieved property information for property_id: {lease.property_id}")
        
        if lease.next_payment_date < date.today():
            logger.info(f"Sending overdue payment message for tenant_id: {tenant_id}")
            message = messages.send_late_message(tenant.phone, tenant.name, lease.next_payment_date, lease.payment_link_url)
        else:
            logger.info(f"Sending payment reminder message for tenant_id: {tenant_id}")
            message = messages.send_reminder_message(tenant.phone, tenant.name, lease.next_payment_date, lease.payment_link_url)
        
        logger.info(f"Message sent successfully to tenant_id: {tenant_id}, SID: {message.sid}")
        return schemas.MessageResponseModel(
            success=True,
            message=f"Message sent successfully. SID: {message.sid}, Message Body: '{message.body}'"
        )
    except ValueError as e:
        logger.error(f"ValueError in send_payment_message_reminder for tenant_id {tenant_id}: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=str(e))
    except TwilioRestException as e:
        logger.error(f"TwilioRestException in send_payment_message_reminder for tenant_id {tenant_id}: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=f"Error sending message via Twilio: {str(e)}")
    except Exception as e:
        logger.exception(f"Unexpected error in send_payment_message_reminder for tenant_id {tenant_id}: {str(e)}")
        return schemas.MessageResponseModel(success=False, message=f"An unexpected error occurred: {str(e)}")

# Event routes
@app.get("/event/{event_id}", status_code=status.HTTP_200_OK, tags=["Event"])
def get_event(event_id: str):
    logger.info(f"Fetching event with id: {event_id}")
    try:
        event = stripe_crud.get_event(event_id)
        if not event:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        logger.info(f"Successfully fetched event with id: {event_id}")
        return event
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"Error fetching event: {str(e)}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

# Health check route
@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
def health_check():
    return {"status": "healthy"}

# Error handling for 404 Not Found
@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.detail},
    )

# Error handling for 500 Internal Server Error
@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"An error occurred: {str(exc)}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"message": "An unexpected error occurred"},
    )
