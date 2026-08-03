"""
Database layer - schema-per-company isolation.

Architecture:
  - One shared Postgres server/database (cheap - no per-company hosting cost).
  - A single "companies" registry table lives in the default (public) schema.
  - Each company gets its OWN Postgres schema (e.g. "company_3") containing
    its own private clients/invoices tables. One company's queries can never
    accidentally return another company's rows, because they're not even
    hitting the same tables under the hood - this is real data isolation,
    not just a filter column that a bug could bypass.
  - get_session() -> use for anything touching the "companies" registry only.
  - get_company_session(company) -> use for anything touching that specific
    company's clients/invoices. Same Client/Invoice model classes are reused
    for every company; only the schema they resolve to changes per session.
"""
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, Integer, String, Float, DateTime, ForeignKey, JSON, Text, text
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config import DATABASE_URL

_db_url = DATABASE_URL
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(_db_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Company(Base):
    """Global registry of onboarded companies. Lives in the default/public schema."""
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)

    # The Telegram group/chat this company operates in - every message from
    # this chat is automatically treated as belonging to this company.
    telegram_chat_id = Column(String, unique=True, nullable=False)

    # The private Postgres schema holding this company's clients/invoices.
    schema_name = Column(String, unique=True, nullable=False)

    paynow_proxy_type = Column(String, default="MOBILE")  # "MOBILE" or "UEN"
    paynow_proxy_value = Column(String, nullable=True)
    bank_name = Column(String, nullable=True)
    beneficiary_name = Column(String, nullable=True)

    # Per-company customization of the one standard invoice layout
    logo_path = Column(String, nullable=True)
    address = Column(String, nullable=True)
    terms_and_conditions = Column(Text, nullable=True)

    # Populated once an admin uploads an example PDF and it's been learned
    template_background_path = Column(String, nullable=True)
    template_field_map = Column(JSON, nullable=True)

    # None = unlimited. Otherwise, hard-blocks new invoices once this many
    # have been generated in the current calendar month.
    invoice_limit_per_month = Column(Integer, nullable=True)

    # Telegram user IDs (besides global ADMIN_TELEGRAM_IDS) allowed to
    # approve/reject THIS company's invoices - typically the client's own
    # business owner, so they self-approve without you in the loop.
    approver_telegram_ids = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)


# "tenant" is a placeholder schema name - it never physically exists in
# Postgres. At query time, schema_translate_map swaps it for the real
# per-company schema (e.g. "company_3"), so the same Client/Invoice classes
# work for every company.
class Client(Base):
    __tablename__ = "clients"
    __table_args__ = {"schema": "tenant"}

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    address = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    invoices = relationship("Invoice", back_populates="client")


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = {"schema": "tenant"}

    id = Column(Integer, primary_key=True)
    doc_type = Column(String, default="invoice")  # "invoice" or "quotation"
    invoice_number = Column(String, unique=True)

    client_id = Column(Integer, ForeignKey("tenant.clients.id"), nullable=True)
    client = relationship("Client", back_populates="invoices")

    items = Column(JSON, nullable=False)   # [{"description","qty","unit_price","amount"}, ...]
    subtotal = Column(Float, default=0.0)
    total = Column(Float, default=0.0)

    raw_transcript = Column(Text, nullable=True)   # original mixed-language transcript
    english_summary = Column(Text, nullable=True)  # translated/cleaned version

    status = Column(String, default="pending_approval")
    # pending_approval -> needs_client_details -> approved -> rejected

    submitted_by = Column(String, nullable=True)   # telegram username/id of staff who sent voice note
    pdf_path = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)


def init_db():
    """Creates only the global companies registry. Per-company tables are
    created on demand in create_company_schema() when a company is onboarded."""
    Company.__table__.create(bind=engine, checkfirst=True)


def get_session():
    """Session for registry-level queries (the companies table only)."""
    return SessionLocal()


def get_company_session(company: Company):
    """Session scoped to one company's private schema - use for all
    Client/Invoice queries."""
    scoped_engine = engine.execution_options(schema_translate_map={"tenant": company.schema_name})
    return sessionmaker(bind=scoped_engine, autoflush=False, autocommit=False)()


def create_company_schema(schema_name: str):
    """Physically creates the Postgres schema and this company's private
    clients/invoices tables inside it."""
    with engine.connect() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"'))
        conn.commit()
    scoped_engine = engine.execution_options(schema_translate_map={"tenant": schema_name})
    Base.metadata.create_all(bind=scoped_engine, tables=[Client.__table__, Invoice.__table__])


def get_company_by_chat(session, chat_id) -> Company:
    return session.query(Company).filter(Company.telegram_chat_id == str(chat_id)).first()
