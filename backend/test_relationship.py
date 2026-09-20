from sqlalchemy import select

from app.database import SessionLocal
from app.models import Tenant, User


db = SessionLocal()

try:
    # Create a tenant
    tenant = Tenant(name="Test Company")
    db.add(tenant)
    db.flush()

    # Create users belonging to that tenant
    user1 = User(
        tenant_id=tenant.id,
        name="Ameya",
        email="ameya@test.com",
        password_hash="test_hash"
    )

    user2 = User(
        tenant_id=tenant.id,
        name="Rahul",
        email="rahul@test.com",
        password_hash="test_hash"
    )

    db.add_all([user1, user2])
    db.commit()

    # Test Tenant → Users
    print("Tenant:", tenant.name)
    print("Users:", [user.name for user in tenant.users])

    # Test User → Tenant
    print("User:", user1.name)
    print("User's Tenant:", user1.tenant.name)

finally:
    db.close()