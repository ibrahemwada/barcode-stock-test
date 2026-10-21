"""Standalone Streamlit app to test offline SQLite barcode lookup.

This app uses its own database file (sme_barcode_test.db) in the same folder.
It does not connect to or modify the user's existing SME app database.
"""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from datetime import datetime

import streamlit as st

try:
    import zxingcpp
    import numpy as np
except ImportError:
    zxingcpp = None
    np = None

try:
    from streamlit_webrtc import webrtc_streamer, WebRtcMode
    import av
except ImportError:
    webrtc_streamer = None
    WebRtcMode = None
    av = None

import streamlit.components.v1 as components
import threading
try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    st_autorefresh = None

APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "sme_barcode_test.db"


@contextmanager
def db():
    """Open a short-lived SQLite connection and close it reliably."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    """Create tables and add barcode column if using an older test DB."""
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT '',
                brand TEXT NOT NULL DEFAULT '',
                variant TEXT NOT NULL DEFAULT '',
                unit TEXT NOT NULL DEFAULT 'piece',
                buy REAL NOT NULL DEFAULT 0,
                sell REAL NOT NULL DEFAULT 0,
                stock REAL NOT NULL DEFAULT 0,
                low_level REAL NOT NULL DEFAULT 5,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row["name"] for row in con.execute("PRAGMA table_info(products)").fetchall()
        }
        if "barcode" not in columns:
            con.execute("ALTER TABLE products ADD COLUMN barcode TEXT")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                movement TEXT NOT NULL,
                quantity REAL NOT NULL,
                unit TEXT NOT NULL,
                unit_cost REAL NOT NULL DEFAULT 0,
                note TEXT NOT NULL DEFAULT '',
                created TEXT NOT NULL,
                FOREIGN KEY(product_id) REFERENCES products(id)
            )
            """
        )
        # Empty barcodes are not treated as identifiers; non-empty barcodes must be unique.
        con.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode_unique
            ON products(barcode)
            WHERE barcode IS NOT NULL AND TRIM(barcode) != ''
            """
        )


def get_products():
    with db() as con:
        return con.execute("SELECT * FROM products ORDER BY name COLLATE NOCASE").fetchall()


def get_product_by_barcode(barcode):
    barcode = (barcode or "").strip()
    if not barcode:
        return None
    with db() as con:
        return con.execute(
            "SELECT * FROM products WHERE barcode = ? LIMIT 1", (barcode,)
        ).fetchone()


def add_movement(con, product_id, movement, quantity, unit, unit_cost, note):
    con.execute(
        """INSERT INTO stock_movements
           (product_id, movement, quantity, unit, unit_cost, note, created)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (product_id, movement, quantity, unit, unit_cost, note, now()),
    )


def product_caption(product):
    parts = [product["name"]]
    if product["brand"]:
        parts.append(product["brand"])
    if product["variant"]:
        parts.append(product["variant"])
    parts.append(f"(barcode: {product['barcode'] or 'not set'})")
    return " — ".join(parts)


def save_stock_received(product, qty, cost, note):
    with db() as con:
        con.execute(
            "UPDATE products SET stock = stock + ?, buy = ? WHERE id = ?",
            (qty, cost, int(product["id"])),
        )
        add_movement(
            con, int(product["id"]), "STOCK IN", qty,
            product["unit"], cost, note.strip()
        )


def render_stock_in_form(product, form_key):
    st.subheader(product["name"])
    details = [
        f"**Current stock:** {product['stock']:g} {product['unit']}",
        f"**Buying price:** ₦{product['buy']:,.2f}",
    ]
    if product["barcode"]:
        details.append(f"**Barcode:** `{product['barcode']}`")
    if product["brand"]:
        details.append(f"**Brand:** {product['brand']}")
    if product["variant"]:
        details.append(f"**Size / variant:** {product['variant']}")
    st.markdown("  |  ".join(details))

    with st.form(form_key):
        qty = st.number_input(
            "Quantity received", min_value=0.001, value=1.0, step=1.0,
            help="Enter the number of units being added."
        )
        cost = st.number_input(
            "Buying cost per unit (₦)", min_value=0.0,
            value=float(product["buy"]), step=50.0
        )
        note = st.text_input("Supplier / note (optional)")
        submitted = st.form_submit_button("Save stock received", type="primary")

    if submitted:
        save_stock_received(product, float(qty), float(cost), note)
        st.session_state["test_notice"] = f"Stock updated for {product['name']}."
        st.rerun()




class LiveBarcodeProcessor:
    """Decode barcodes continuously from live webcam frames."""
    def __init__(self, result_queue):
        self.result_queue = result_queue
        self.last_value = ""
        self.last_time = 0.0
        self.lock = threading.Lock()

    def recv(self, frame):
        image = frame.to_ndarray(format="rgb24")
        if zxingcpp is not None and np is not None:
            try:
                results = zxingcpp.read_barcodes(image)
                if results:
                    value = next((r.text.strip() for r in results if r.text.strip()), "")
                    if value:
                        import time
                        with self.lock:
                            # Queue each same barcode only once every 2 seconds.
                            if value != self.last_value or time.time() - self.last_time > 2:
                                self.result_queue.put(value)
                                self.last_value = value
                                self.last_time = time.time()
            except Exception:
                pass
        return frame


def play_beep():
    """Play a short beep in the browser after a barcode is detected."""
    components.html(
        """<script>
        const Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx) {
          const ctx = new Ctx();
          const osc = ctx.createOscillator();
          const gain = ctx.createGain();
          osc.type = 'sine'; osc.frequency.value = 880;
          gain.gain.value = 0.15;
          osc.connect(gain); gain.connect(ctx.destination);
          osc.start(); osc.stop(ctx.currentTime + 0.16);
        }
        </script>""",
        height=0,
    )


def camera_barcode_input(prefix, barcode_state_key):
    """Optional live video barcode scanner with explicit open/close controls."""
    st.markdown("**Live camera scanner**")
    if webrtc_streamer is None or zxingcpp is None or np is None:
        st.warning(
            "Live camera packages are missing. Install the updated requirements file, "
            "then restart the app."
        )
        return

    open_key = f"{prefix}_camera_open"
    if open_key not in st.session_state:
        st.session_state[open_key] = False

    col_open, col_close = st.columns(2)
    with col_open:
        if st.button("📷 Open Camera", key=f"{prefix}_open_button", disabled=st.session_state[open_key], use_container_width=True):
            st.session_state[open_key] = True
            st.rerun()
    with col_close:
        if st.button("⏹ Close Camera", key=f"{prefix}_close_button", disabled=not st.session_state[open_key], use_container_width=True):
            st.session_state[open_key] = False
            st.rerun()

    if st.session_state[open_key]:
        # Poll the thread-safe result queue so live detections appear in the UI.
        if st_autorefresh is not None:
            st_autorefresh(interval=500, key=f"{prefix}_camera_poll")
        if f"{prefix}_queue" not in st.session_state:
            import queue
            st.session_state[f"{prefix}_queue"] = queue.Queue()
        result_queue = st.session_state[f"{prefix}_queue"]
        st.caption("Live video is active. Hold one barcode steady in the frame. A beep sounds when it is detected.")
        webrtc_streamer(
            key=f"{prefix}_live_video",
            mode=WebRtcMode.SENDRECV,
            video_processor_factory=lambda: LiveBarcodeProcessor(result_queue),
            media_stream_constraints={"video": True, "audio": False},
            async_processing=True,
        )
        # Read any decoded value queued by the video processing thread.
        latest = None
        try:
            while True:
                latest = result_queue.get_nowait()
        except Exception:
            pass
        if latest:
            previous = st.session_state.get(f"{prefix}_last_detected")
            st.session_state[f"{prefix}_last_detected"] = latest
            if st.session_state.get(barcode_state_key) != latest:
                st.session_state[barcode_state_key] = latest
                st.session_state[f"{prefix}_beep_pending"] = True
                st.rerun()
            elif previous != latest:
                st.session_state[f"{prefix}_beep_pending"] = True

    if st.session_state.pop(f"{prefix}_beep_pending", False):
        play_beep()
        st.success(f"Barcode captured: {st.session_state.get(barcode_state_key, '')}")

def main():
    st.set_page_config(page_title="Barcode + SQLite Test", page_icon="📦", layout="centered")
    init_db()

    st.title("📦 Barcode + SQLite Test")
    st.caption("Standalone test app — local SQLite lookup works offline. Camera access requires browser permission; the app can be opened on your laptop or phone browser.")
    st.info(f"Test database file: `{DB_PATH.name}`. This is separate from your main SME app database.")

    notice = st.session_state.pop("test_notice", None)
    if notice:
        st.success(notice)

    mode = st.radio(
        "Choose what you want to do",
        ["Add to Existing Stock", "Register New Stock"],
        horizontal=True,
    )

    if mode == "Add to Existing Stock":
        st.subheader("Add to Existing Stock")
        camera_barcode_input("existing", "existing_barcode")
        barcode = st.text_input(
            "Or enter barcode manually (USB/Bluetooth scanners also work)",
            key="existing_barcode",
            help="A USB/Bluetooth scanner usually types the barcode like a keyboard. Press Enter if needed.",
        ).strip()

        product = None
        if barcode:
            product = get_product_by_barcode(barcode)
            if product:
                st.success("Barcode found in the local SQLite database.")
                render_stock_in_form(product, f"stock_in_barcode_{product['id']}")
            else:
                st.warning("Barcode not found locally. Choose ‘Register New Stock’ to register this barcode first.")

        if not barcode:
            products = get_products()
            if not products:
                st.info("No products saved yet. Go to ‘Register New Stock’ to create your first test product.")
            else:
                product_map = {product_caption(p): p for p in products}
                choice = st.selectbox(
                    "Or select a product by name",
                    list(product_map.keys()),
                    index=None,
                    placeholder="Type to search products…",
                )
                if choice:
                    render_stock_in_form(product_map[choice], f"stock_in_manual_{product_map[choice]['id']}")

    else:
        st.subheader("Register New Stock")
        camera_barcode_input("new_product", "new_product_barcode")
        barcode = st.text_input(
            "Or enter barcode manually (optional)",
            key="new_product_barcode",
            help="A USB/Bluetooth scanner also works here. The barcode is saved locally for future offline scans.",
        ).strip()

        duplicate = get_product_by_barcode(barcode) if barcode else None
        if duplicate:
            st.error(
                f"This barcode is already registered to ‘{duplicate['name']}’. "
                "Switch to ‘Add to Existing Stock’ to add quantity."
            )

        with st.form("register_product_form", clear_on_submit=False):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("Product name *")
                category = st.selectbox(
                    "Category",
                    ["Provisions", "Textiles", "Pharmacy", "Electronics", "Others"],
                )
                brand = st.text_input("Brand (optional)")
                unit = st.selectbox(
                    "Stock unit",
                    ["piece", "pack", "carton", "bag", "crate", "bottle", "sachet", "kg", "g", "litre", "ml"],
                )
                buy = st.number_input("Buying price per unit (₦)", min_value=0.0, step=50.0)
            with col2:
                variant = st.text_input("Size / variant (e.g. 50g, 1 litre)")
                qty = st.number_input("Opening quantity", min_value=0.0, value=1.0, step=1.0)
                sell = st.number_input("Selling price per unit (₦)", min_value=0.0, step=50.0)
                low_level = st.number_input("Low-stock alert level", min_value=0.0, value=5.0, step=1.0)

            save = st.form_submit_button("💾 Register product", type="primary", use_container_width=True)

        if save:
            if not name.strip():
                st.error("Product name is required.")
            elif buy <= 0 or sell <= 0:
                st.error("Enter a buying price and selling price greater than zero.")
            else:
                # Recheck at save time to prevent duplicate barcode records.
                latest_duplicate = get_product_by_barcode(barcode) if barcode else None
                if latest_duplicate:
                    st.error(
                        f"Cannot register: barcode already belongs to ‘{latest_duplicate['name']}’. "
                        "Use ‘Add to Existing Stock’ instead."
                    )
                else:
                    try:
                        with db() as con:
                            cur = con.execute(
                                """INSERT INTO products
                                (name, category, brand, variant, unit, buy, sell,
                                 stock, low_level, created, barcode)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                (
                                    name.strip(), category, brand.strip(), variant.strip(),
                                    unit, float(buy), float(sell), float(qty),
                                    float(low_level), now(), barcode or None,
                                ),
                            )
                            if qty > 0:
                                add_movement(
                                    con, int(cur.lastrowid), "STOCK IN", float(qty),
                                    unit, float(buy), "New product registered"
                                )
                        st.session_state["test_notice"] = f"{name.strip()} registered in the local SQLite database."
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("That barcode is already in the database. Please look it up under ‘Add to Existing Stock’. ")

    st.divider()
    st.subheader("Local product catalogue")
    products = get_products()
    if not products:
        st.caption("No products saved yet.")
    else:
        rows = [
            {
                "Product": p["name"],
                "Barcode": p["barcode"] or "",
                "Brand": p["brand"],
                "Size / variant": p["variant"],
                "Unit": p["unit"],
                "Buying price (₦)": p["buy"],
                "Selling price (₦)": p["sell"],
                "Stock": p["stock"],
            }
            for p in products
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

    with st.expander("View stock history"):
        with db() as con:
            history = con.execute(
                """SELECT p.name AS Product, p.barcode AS Barcode,
                          m.movement AS Movement, m.quantity AS Quantity,
                          m.unit AS Unit, m.unit_cost AS 'Unit cost (₦)',
                          m.note AS Note, m.created AS Date
                   FROM stock_movements m
                   JOIN products p ON p.id = m.product_id
                   ORDER BY m.id DESC"""
            ).fetchall()
        if history:
            st.dataframe([dict(row) for row in history], use_container_width=True, hide_index=True)
        else:
            st.caption("No stock movements yet.")


if __name__ == "__main__":
    main()
