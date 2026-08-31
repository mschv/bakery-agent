import streamlit as st
from google.genai import types

from agent import (
    create_chat_session,
    get_accounts_receivable,
    get_financial_summary,
    get_inventory_status,
    get_open_orders,
    transcribe_audio,
)

FILE_MIME_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "pdf": "application/pdf",
}


def build_morning_briefing(inv_data: dict) -> str:
    """Builds a proactive greeting from live inventory data instead of a fixed message."""
    expiring = inv_data.get("expiring_soon", [])
    low_stock = inv_data.get("low_stock", [])

    if not expiring and not low_stock:
        return (
            "Good morning! ☀️ Inventory looks steady — nothing expiring soon "
            "and no low-stock alerts. What would you like to work on today?"
        )

    lines = ["Good morning! ☀️ Here's where things stand:"]
    if expiring:
        items = "; ".join(
            f"**{item.get('stock_qty')} {item.get('unit')} of {item.get('name')}** "
            f"(expires {item.get('expiration_date')})"
            for item in expiring
        )
        lines.append(f"- Expiring soon: {items}")
    if low_stock:
        items = "; ".join(
            f"**{item.get('name')}** ({item.get('stock_qty')} {item.get('unit')} left)"
            for item in low_stock
        )
        lines.append(f"- Running low: {items}")
    lines.append("Want to plan around any of this?")
    return "\n".join(lines)


# Page Setup
st.set_page_config(page_title="Bakery Partner Co-Pilot", page_icon="🥐", layout="wide")

# App Header
st.title("🥐 Bakery Partner Co-Pilot")
st.caption("AI Operations Partner & Live Inventory Manager")

# Fetch live inventory once per rerun and reuse it across both panels
inv_data = get_inventory_status()

# Create Two-Panel Grid Layout
left_col, right_col = st.columns([6, 4], gap="large")

# =========================================================
# LEFT PANEL: Collaborative Partner Chat Interface
# =========================================================
with left_col:
    st.subheader("💬 Co-Pilot Workspace")

    # Initialize Chat History with a Proactive Morning Briefing built from live data
    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": build_morning_briefing(inv_data)}
        ]

    # Initialize a persistent chat session (kept in memory for this browser
    # session) so the model remembers prior turns instead of treating every
    # message as brand new.
    if "chat" not in st.session_state:
        st.session_state.chat = create_chat_session()

    # Render Chat History inside a fixed-height, independently scrollable
    # container so a long conversation doesn't push the input off-screen.
    chat_container = st.container(height=520)
    with chat_container:
        for message in st.session_state.messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

    def process_user_turn(contents, display_text):
        """Sends `contents` (a plain string, or a list of Parts for
        multimodal input) to the chat session, showing `display_text` as the
        user's chat bubble. Shared by text, voice, and file-attachment input
        so all three get identical tool-calling behavior and error handling.
        """
        st.session_state.messages.append({"role": "user", "content": display_text})
        with chat_container:
            with st.chat_message("user"):
                st.markdown(display_text)

            with st.chat_message("assistant"):
                with st.spinner("Processing operation tools..."):
                    try:
                        response = st.session_state.chat.send_message(contents)
                        response_text = response.text or "Done! I updated the operations data."
                    except Exception:
                        response_text = (
                            "Sorry, I couldn't process that — there may be a connection "
                            "issue. Please try again."
                        )
                    st.markdown(response_text)

                    st.session_state.messages.append(
                        {"role": "assistant", "content": response_text}
                    )

                    # Rerun to refresh live dashboard metrics on right panel
                    st.rerun()

    # Voice + attachment row. Both widgets get a session-tracked key that
    # increments after each use, so Streamlit resets them to empty on the
    # next render instead of re-sending the same file/recording forever.
    if "uploader_key" not in st.session_state:
        st.session_state.uploader_key = 0
    if "audio_key" not in st.session_state:
        st.session_state.audio_key = 0

    attach_col, voice_col = st.columns(2)
    with attach_col:
        uploaded_file = st.file_uploader(
            "📎 Attach a photo or PDF",
            type=list(FILE_MIME_TYPES.keys()),
            key=f"file_uploader_{st.session_state.uploader_key}",
            label_visibility="collapsed",
        )
    with voice_col:
        audio = st.audio_input(
            "🎤 Or record a voice message",
            key=f"audio_input_{st.session_state.audio_key}",
            label_visibility="collapsed",
        )

    if uploaded_file is not None:
        st.session_state.uploader_key += 1
        ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
        mime_type = FILE_MIME_TYPES.get(ext, "application/octet-stream")
        caption = f"📎 Attached: {uploaded_file.name}"
        process_user_turn(
            [
                types.Part.from_text(text=caption),
                types.Part.from_bytes(data=uploaded_file.getvalue(), mime_type=mime_type),
            ],
            caption,
        )
    elif audio is not None:
        st.session_state.audio_key += 1
        try:
            with st.spinner("Transcribing..."):
                transcript = transcribe_audio(audio.getvalue())
        except Exception:
            transcript = None
        if transcript:
            process_user_turn(transcript, f"🎤 {transcript}")
        else:
            st.warning("Couldn't transcribe that recording — try again.")

    # Text Input Field
    if prompt := st.chat_input("Ask inventory questions, log usage, or give preferences..."):
        process_user_turn(prompt, prompt)

# =========================================================
# RIGHT PANEL: Live Bakery Operations Dashboard
# =========================================================
with right_col:
    st.subheader("📊 Live Operations Panel")

    # 1. Open Orders — what's due, the most actionable thing on the dashboard
    st.markdown("### 📋 Open Orders")
    open_orders = get_open_orders()
    if isinstance(open_orders, list) and open_orders:
        for order in open_orders:
            due = order.get("due_date") or "no due date"
            price = f"${order['price']}" if order.get("price") is not None else "price TBD"
            status = order.get("status", "pending")
            st.write(
                f"**{order.get('customer', '?')}** — {order.get('quantity', 1)}x "
                f"{order.get('item', '?')} · due {due} · {price} · _{status}_"
            )
    else:
        st.caption("No open orders.")

    st.divider()

    # 2. Money Snapshot — this week's revenue/cost/net plus what's owed
    st.markdown("### 💰 This Week")
    summary = get_financial_summary(days=7)
    ar = get_accounts_receivable()
    if isinstance(summary, dict) and "revenue" in summary:
        m1, m2, m3 = st.columns(3)
        m1.metric("Revenue", f"${summary['revenue']}")
        m2.metric("Costs", f"${summary['total_cost']}")
        m3.metric("Net", f"${summary['net']}")
        owed = ar.get("total_owed", 0) if isinstance(ar, dict) else 0
        st.caption(f"{summary.get('sales_count', 0)} sales this week · ${owed} owed from open orders")
    else:
        st.caption("Financial summary unavailable.")

    st.divider()

    # 3. Inventory Levels — expiring items and low stock both get visual weight
    st.markdown("### 📦 Inventory Levels")
    expiring_ids = {item.get("id") for item in inv_data.get("expiring_soon", [])}
    low_stock_ids = {item.get("id") for item in inv_data.get("low_stock", [])}

    for item in inv_data.get("expiring_soon", []):
        st.error(
            f"⚠️ **{item['name']}**: {item['stock_qty']} {item['unit']} (Expires: {item['expiration_date']})"
        )

    for item in inv_data.get("all_ingredients", []):
        if item.get("id") in expiring_ids:
            continue
        if item.get("id") in low_stock_ids:
            st.warning(f"🔻 **{item['name']}**: {item['stock_qty']} {item['unit']} (below reorder threshold)")
        else:
            st.write(f"• **{item['name']}**: {item['stock_qty']} {item['unit']}")