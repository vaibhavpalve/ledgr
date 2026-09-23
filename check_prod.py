import asyncio, os, asyncpg

async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_ADMIN_URL"])
    has = lambda s: conn.fetchval(s)
    print("tracking table:", await has("select to_regclass('app.schema_migrations') is not null"))
    print("0054 receivable_items:", await has("select exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='invoicing' and proname='receivable_items')"))
    print("0061 sales_invoice_batch:", await has("select to_regclass('public.sales_invoice_batch') is not null"))
    print("0062 balances_as_of:", await has("select exists(select 1 from pg_proc p join pg_namespace n on n.oid=p.pronamespace where n.nspname='ledger' and proname='balances_as_of')"))
    await conn.close()
