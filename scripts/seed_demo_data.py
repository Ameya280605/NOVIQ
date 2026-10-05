from backend.app.database import SessionLocal
from backend.app.models import Tenant, User
from backend.app.utils.security import hash_password


db = SessionLocal()


tenant2 = db.query(Tenant).filter(Tenant.id == 2).first()
tenant3 = db.query(Tenant).filter(Tenant.id == 3).first()
tenant4 = db.query(Tenant).filter(Tenant.id == 4).first()

if not tenant2 or not tenant3 or not tenant4:
    raise RuntimeError("One or more demo tenants were not found")


owner2 = db.query(User).filter(
    User.email == "vikram@cloudsphere.example"
).first()

if not owner2:
    owner2 = User(
        tenant_id=tenant2.id,
        name="Vikram Desai",
        email="vikram@cloudsphere.example",
        password_hash=hash_password("Test@12345"),
        role="owner",
    )

    db.add(owner2)
    

owner3 = db.query(User).filter(
    User.email == "sneha@dataforge.example"
).first()

if not owner3:
    owner3 = User(
        tenant_id=tenant3.id,
        name="Sneha Patil",
        email="sneha@dataforge.example",
        password_hash=hash_password("Test@12345"),
        role="owner",
    )

    db.add(owner3)
    

owner4 = db.query(User).filter(
    User.email == "aditya@nexora.example"
).first()

if not owner4:
    owner4 = User(
        tenant_id=tenant4.id,
        name="Aditya Shah",
        email="aditya@nexora.example",
        password_hash=hash_password("Test@12345"),
        role="owner",
    )

    db.add(owner4)
    

db.commit()


db.close()