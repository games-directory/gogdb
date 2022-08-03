import json
import sqlite3
import dataclasses
import os
import collections
import logging

import aiosqlite

from gogdb.core.normalization import normalize_search, compress_systems
import gogdb.core.storage as storage
import gogdb.core.model as model



logger = logging.getLogger("UpdateDB.Index")

async def init_db(cur):
    await cur.execute("""CREATE TABLE products (
        product_id INTEGER,
        title TEXT,
        image_logo TEXT,
        product_type TEXT,
        comp_systems TEXT,
        sale_rank INTEGER,
        search_title TEXT
    );""")
    await cur.execute("""CREATE TABLE changelog (
        product_id INTEGER,
        product_title TEXT,
        timestamp REAL,
        action TEXT,
        category TEXT,
        dl_type TEXT,
        bonus_type TEXT,
        property_name TEXT,
        serialized_record TEXT
    );""")
    await cur.execute("""CREATE TABLE changelog_summary (
        product_id INTEGER,
        product_title TEXT,
        timestamp REAL,
        categories TEXT
    );""")
    await cur.execute("CREATE INDEX idx_products_sale_rank ON products (sale_rank)")
    await cur.execute("CREATE INDEX idx_changelog_timestamp ON changelog (timestamp)")
    await cur.execute("CREATE INDEX idx_summary_timestamp ON changelog_summary (timestamp)")

async def count_rows(cur, table_name):
    await cur.execute(f"SELECT COUNT(*) FROM {table_name};")
    return (await cur.fetchone())[0]

async def index_product(prod, cur, num_ids):
    if prod.rank_bestselling is not None:
        sale_rank = num_ids - prod.rank_bestselling + 1
    else:
        sale_rank = 0
    await cur.execute(
        "INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            prod.id,
            prod.title,
            prod.image_logo,
            prod.type,
            compress_systems(prod.comp_systems),
            sale_rank,
            normalize_search(prod.title)
        )
    )

async def index_changelog(prod, changelog, cur):
    summaries = collections.defaultdict(set)
    for changerec in changelog:
        idx_change = model.IndexChange(
            id = prod.id,
            title = prod.title,
            timestamp = changerec.timestamp,
            action = changerec.action,
            category = changerec.category,
            record = changerec
        )
        if changerec.category == "download":
            idx_change.dl_type = changerec.download_record.dl_type
            if changerec.download_record.dl_new_bonus is not None:
                idx_change.bonus_type = changerec.download_record.dl_new_bonus.bonus_type
            if changerec.download_record.dl_old_bonus is not None:
                # Just set it potentially twice because it has to be the same value anyway
                idx_change.bonus_type = changerec.download_record.dl_old_bonus.bonus_type
        elif changerec.category == "property":
            idx_change.property_name = changerec.property_record.property_name

        await cur.execute(
            "INSERT INTO changelog VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                idx_change.id,
                idx_change.title,
                idx_change.timestamp.timestamp(),
                idx_change.action,
                idx_change.category,
                idx_change.dl_type,
                idx_change.bonus_type,
                idx_change.property_name,
                json.dumps(
                    idx_change.record, sort_keys=True, ensure_ascii=False,
                    default=storage.json_encoder)
            )
        )

        summaries[changerec.timestamp].add(changerec.category)

    for timestamp, category_set in summaries.items():
        category_str = ",".join(sorted(category_set))
        await cur.execute(
            "INSERT INTO changelog_summary VALUES (?, ?, ?, ?)",
            (
                prod.id,
                prod.title,
                timestamp.timestamp(),
                category_str
            )
        )

async def index_main(db):
    ids = await db.ids.load()
    print(f"Starting indexer with {len(ids)} IDs")

    changelog_index_path = db.path_indexdb()
    changelog_index_path.parent.mkdir(exist_ok=True)
    need_create = not changelog_index_path.exists()
    conn = await aiosqlite.connect(changelog_index_path, isolation_level=None)
    cur = await conn.cursor()
    if need_create:
        await init_db(cur)

    await cur.execute("BEGIN TRANSACTION;")
    await cur.execute("DELETE FROM products;")
    await cur.execute("DELETE FROM changelog;")
    await cur.execute("DELETE FROM changelog_summary;")

    num_ids = len(ids)
    for prod_id in ids:
        logger.info(f"Adding {prod_id}")
        prod = await db.product.load(prod_id)
        if prod is None:
            logger.info(f"Skipped {prod_id}")
            continue
        await index_product(prod, cur, num_ids)

        changelog = await db.changelog.load(prod_id)
        if changelog is None:
            logger.info(f"No changelog {prod_id}")
            continue
        await index_changelog(prod, changelog, cur)

    await cur.execute("END TRANSACTION;")

    print("Indexed {} products, {} changelog entries, {} changelog summaries".format(
        await count_rows(cur, "products"),
        await count_rows(cur, "changelog"),
        await count_rows(cur, "changelog_summary")
    ))

    await cur.close()
    await conn.commit()
    await conn.close()
