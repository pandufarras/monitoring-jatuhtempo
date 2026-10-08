import datetime
import json
import pandas as pd
import requests
import streamlit as st
import urllib3

# Nonaktifkan warning SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- KONFIGURASI HALAMAN ---
st.set_page_config(
    page_title="Monitoring Kiriman Jatuh Tempo",
    page_icon="📦",
    layout="wide"
)

# Endpoint & Kredensial Elasticsearch Mile App (Aman dengan fallback secrets)
URL = "https://board.mile.app/elasticsearch/expos.package_connote.pos.*/_search"
HEADERS = {"Content-Type": "application/json", "kbn-xsrf": "true"}

AUTH_USER = st.secrets.get("ES_USER", "upt")
AUTH_PASS = st.secrets.get("ES_PASS", "posind3m4s")
AUTH = (AUTH_USER, AUTH_PASS)


def build_payload(kc_code: str, start_date: datetime.date, end_date: datetime.date, max_size: int = 5000) -> dict:
    """Membangun query pencarian Elasticsearch untuk rentang tanggal Jatuh Tempo."""
    prev_start = start_date - datetime.timedelta(days=1)
    gte_utc = f"{prev_start.strftime('%Y-%m-%d')}T17:00:00.000Z"
    lte_utc = f"{end_date.strftime('%Y-%m-%d')}T16:59:59.999Z"

    return {
        "size": max_size,
        "query": {
            "bool": {
                "filter": [
                    {
                        "bool": {
                            "minimum_should_match": 1,
                            "should": [
                                {"match_phrase": {"connote.connote_service.keyword": "Q9"}},
                                {"match_phrase": {"connote.connote_service.keyword": "PE"}},
                                {"match_phrase": {"connote.connote_service.keyword": "PKH"}},
                                {"match_phrase": {"connote.connote_service.keyword": "EC3"}},
                            ],
                        }
                    },
                    {
                        "bool": {
                            "filter": [
                                {
                                    "bool": {
                                        "minimum_should_match": 1,
                                        "should": [
                                            {"query_string": {"fields": ["connote.connote_receiver_zipcode.keyword"], "query": "\\6*"}},
                                            {"query_string": {"fields": ["connote.connote_receiver_zipcode.keyword"], "query": "\\8*"}},
                                        ],
                                    }
                                }
                            ]
                        }
                    },
                    {"match_phrase": {"custom_field.destination_kprk.keyword": {"query": str(kc_code)}}},
                    {
                        "range": {
                            "custom_field.final_swp_date_new": {
                                "format": "strict_date_optional_time",
                                "gte": gte_utc,
                                "lte": lte_utc,
                            }
                        }
                    },
                ],
                "must_not": [
                    {
                        "bool": {
                            "should": [
                                {"match_phrase": {"connote.connote_state.keyword": "DELIVERED"}},
                                {"match_phrase": {"connote.connote_state.keyword": "DELIVERED (RETURN DELIVERY)"}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    {
                        "bool": {
                            "should": [
                                {"match_phrase": {"connote.connote_state.keyword": "CANCEL"}},
                                {"match_phrase": {"connote.connote_state.keyword": "PENDING"}},
                            ],
                            "minimum_should_match": 1,
                        }
                    },
                    {"match_phrase": {"connote.location_name.keyword": {"query": "AGP TESTING LOCATION"}}},
                    {"match_phrase": {"connote.connote_service.keyword": {"query": "LNINCOMING"}}},
                ],
            }
        },
    }


def format_tgl_update(raw_dt) -> str:
    """Mengonversi timestamp ISO ke format 'dd/mm/yyyy HH.MM' WIB."""
    if not raw_dt:
        return "-"
    try:
        dt = pd.to_datetime(raw_dt)
        if dt.tzinfo is not None:
            dt = dt.tz_convert("Asia/Jakarta")
        return dt.strftime("%d/%m/%Y %H.%M")
    except Exception:
        return str(raw_dt).replace("T", " ")[:16]


def extract_jatuh_tempo_date_wib(raw_swp) -> datetime.date:
    """Mengambil tanggal kalender Jatuh Tempo dalam zona WIB."""
    if not raw_swp:
        return None
    try:
        dt = pd.to_datetime(raw_swp)
        if dt.tzinfo is not None:
            dt = dt.tz_convert("Asia/Jakarta")
        else:
            dt = dt + pd.Timedelta(hours=7)
        return dt.date()
    except Exception:
        return None


def extract_petugas(src: dict) -> str:
    """Mengekstrak nama petugas update sesuai mapping Kibana (currentLocation.full_name)."""
    if not isinstance(src, dict):
        return "-"

    curr_loc = src.get("currentLocation") or {}
    custom = src.get("custom_field") or {}
    connote = src.get("connote") or {}
    pod = src.get("pod") or {}

    petugas = curr_loc.get("full_name")
    if petugas and str(petugas).strip() and str(petugas).strip() not in ["__missing__", "-"]:
        return str(petugas).strip()

    petugas = (
        curr_loc.get("user_name")
        or curr_loc.get("actor_name")
        or curr_loc.get("actor")
        or custom.get("first_attempt_courier_name")
        or custom.get("courier_name")
        or custom.get("updated_by_name")
        or pod.get("courier_name")
        or connote.get("user_name")
    )
    if petugas and str(petugas).strip() and str(petugas).strip() not in ["__missing__", "-"]:
        return str(petugas).strip()

    hist_tracking = custom.get("history_tracking")
    if isinstance(hist_tracking, list):
        for item in reversed(hist_tracking):
            if isinstance(item, dict):
                p = item.get("user_name") or item.get("actor_name") or item.get("actor")
                if p and str(p).strip():
                    return str(p).strip()

    return "-"


@st.cache_data(ttl=120)
def fetch_monitoring_data(kc_code: str, target_date: datetime.date) -> pd.DataFrame:
    """
    Mengambil data:
    1. Target Date (Hari Ini): Diambil SEMUA (Dalam Kendali + Di Luar Kendali).
    2. 6 Hari Sebelum Target Date: HANYA diambil yang Dalam Kendali (692xx).
    3. ATURAN IRREGULARITY RETUR BARANG:
       Jika ada irregularity Retur Barang, HANYA dimasukkan jika masih di nopen 692xx (Dalam Kendali).
       Jika sudah di luar 692xx, tidak dimasukkan.
    """
    start_date = target_date - datetime.timedelta(days=6)
    payload = build_payload(kc_code, start_date, target_date)

    try:
        resp = requests.post(
            URL,
            headers=HEADERS,
            auth=AUTH,
            json=payload,
            verify=False,
            timeout=45,
        )
    except Exception as e:
        st.error(f"Gagal menghubungkan ke server Elasticsearch: {e}")
        return pd.DataFrame()

    if resp.status_code != 200:
        st.error(f"Gagal mengambil data dari server (HTTP {resp.status_code}): {resp.text[:300]}")
        return pd.DataFrame()

    hits = resp.json().get("hits", {}).get("hits", [])
    if not hits:
        return pd.DataFrame()

    rows = []
    for h in hits:
        src = h.get("_source") or {}
        connote = src.get("connote") or {}
        custom = src.get("custom_field") or {}
        curr_loc = src.get("currentLocation") or {}

        loc_name = curr_loc.get("name") or "-"
        loc_code = str(curr_loc.get("code") or curr_loc.get("location_id") or "")

        # Klasifikasi Kendali (Awalan 692)
        is_dalam_kendali = loc_code.startswith("692") or ("692" in loc_name)
        status_kendali = "Dalam Kendali" if is_dalam_kendali else "Di Luar Kendali"

        raw_swp = custom.get("final_swp_date_new")
        jt_date = extract_jatuh_tempo_date_wib(raw_swp)

        # 1. Aturan 6 Hari Lalu: hanya yang Dalam Kendali
        if jt_date and jt_date < target_date:
            if not is_dalam_kendali:
                continue

        # 2. Aturan Irregularity Retur Barang:
        # Cek apakah kiriman berstatus retur barang pada irregularity
        irreg_reason = str(custom.get("irregularityReason") or custom.get("irregularity_reason") or "").lower()
        irreg_status = str(custom.get("irregularityStatus") or custom.get("irregularity_status") or "").lower()
        connote_state = str(connote.get("connote_state") or "").lower()

        is_retur_barang = ("retur" in irreg_reason) or ("retur" in irreg_status) or ("return" in connote_state)

        # Jika sudah berstatus retur dan posisinya di LUAR kendali (bukan 692), lewati/skip
        if is_retur_barang and not is_dalam_kendali:
            continue

        # Penentuan Status SLA
        over_sla_flag = custom.get("over_sla")
        sla_state = str(custom.get("sla_state") or "").lower()

        if jt_date and jt_date < target_date:
            status_sla = "Over SLA"
        elif over_sla_flag == 1 or over_sla_flag == "1" or "over" in sla_state:
            status_sla = "Over SLA"
        else:
            status_sla = "Jatuh Tempo"

        petugas_name = extract_petugas(src)
        raw_updated = connote.get("updated_at") or src.get("updated_at")
        tgl_update = format_tgl_update(raw_updated)

        tgl_jt_label = jt_date.strftime("%d/%m/%Y") if jt_date else "-"
        kategori_hari = "Hari Ini" if jt_date == target_date else "6 Hari Lalu"

        rows.append({
            "connote": connote.get("connote_code") or src.get("connote_code") or h.get("_id"),
            "KC/KCP": loc_name,
            "Tgl Jatuh Tempo": tgl_jt_label,
            "Periode": kategori_hari,
            "Status SLA": status_sla,
            "Tgl Update": tgl_update,
            "Petugas Update": petugas_name,
            "Status": connote.get("connote_state") or "-",
            "Layanan": connote.get("connote_service") or "-",
            "Penerima": connote.get("connote_receiver_name") or "-",
            "Alamat": connote.get("connote_receiver_address") or "-",
            "First Attempt": custom.get("first_attempt_time") or "-",
            "Alasan Gagal Antar": custom.get("reason_failedtodelivered") or "-",
            "Irregularity": custom.get("irregularityReason") or "-",
            "Kendali": status_kendali,
        })

    df = pd.DataFrame(rows)
    return df


def render_metric_card(title: str, value: int, badge_text: str = "", badge_bg: str = "#e2e8f0", badge_color: str = "#334155", card_border: str = "#e2e8f0", icon_char: str = ""):
    """Merender kartu indikator modern dengan angka ukuran besar."""
    badge_html = f'<div style="display: inline-block; background-color: {badge_bg}; color: {badge_color}; font-size: 11px; font-weight: 700; padding: 3px 8px; border-radius: 999px; margin-top: 6px;">{badge_text}</div>' if badge_text else ''
    card_html = f"""
    <div style="background-color: #ffffff; border-radius: 12px; border: 1px solid {card_border}; padding: 16px 14px; box-shadow: 0 2px 6px rgba(0,0,0,0.04); height: 100%; display: flex; flex-direction: column; justify-content: space-between;">
        <div style="font-size: 13px; font-weight: 600; color: #475569; display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
            <span>{title}</span>
            <span style="font-size: 16px;">{icon_char}</span>
        </div>
        <div style="font-size: 38px; font-weight: 900; color: #0f172a; line-height: 1.1; margin: 4px 0 2px 0; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;">
            {value}
        </div>
        <div>
            {badge_html}
        </div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)


def render_screenshot_card(courier_name: str, group_df: pd.DataFrame, header_bg: str = "#002060"):
    """Merender tabel HTML siap screenshot per pengantar rapat tanpa indentasi."""
    rows_list = []
    for _, row in group_df.iterrows():
        r_connote = str(row['connote'])
        r_loc = str(row['KC/KCP'])
        r_tgl = str(row['Tgl Update'])
        r_petugas = str(row['Petugas Update'])
        r_status = str(row['Status'])
        r_layanan = str(row['Layanan'])
        r_sla = str(row['Status SLA'])
        r_penerima = str(row['Penerima'])
        r_alamat = str(row['Alamat'])

        sla_color = "#b30000" if r_sla == "Over SLA" else "#d97706"

        row_html = (
            f'<tr style="border-bottom: 1px solid #ddd; background-color: #ffffff; color: #111111; font-size: 13px;">'
            f'<td style="padding: 7px 10px; font-weight: bold; border-right: 1px solid #eee;">{r_connote}</td>'
            f'<td style="padding: 7px 10px; border-right: 1px solid #eee;">{r_loc}</td>'
            f'<td style="padding: 7px 10px; text-align: center; border-right: 1px solid #eee;">{r_tgl}</td>'
            f'<td style="padding: 7px 10px; border-right: 1px solid #eee;">{r_petugas}</td>'
            f'<td style="padding: 7px 10px; text-align: center; border-right: 1px solid #eee; font-weight: 600;">{r_status}</td>'
            f'<td style="padding: 7px 10px; text-align: center; border-right: 1px solid #eee; font-weight: bold; color: {header_bg};">{r_layanan}</td>'
            f'<td style="padding: 7px 10px; text-align: center; border-right: 1px solid #eee; font-weight: 700; color: {sla_color};">{r_sla}</td>'
            f'<td style="padding: 7px 10px; border-right: 1px solid #eee;">{r_penerima}</td>'
            f'<td style="padding: 7px 10px;">{r_alamat}</td>'
            f'</tr>'
        )
        rows_list.append(row_html)

    tbody_content = "".join(rows_list)

    full_card_html = (
        f'<div style="margin-bottom: 25px; border-radius: 4px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,0.15); border: 1px solid #bbb;">'
        f'<div style="background-color: #ffffff; color: #000000; text-align: center; padding: 7px; font-size: 18px; font-weight: 900; letter-spacing: 0.5px; border-bottom: 2px solid #111;">'
        f'{courier_name}'
        f'</div>'
        f'<table style="width: 100%; border-collapse: collapse; font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif;">'
        f'<thead>'
        f'<tr style="background-color: {header_bg}; color: #ffffff; font-size: 13px; text-align: center;">'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Nomor Resi</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Posisi Saat ini</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Tgl Update</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Petugas Update</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Status</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Produk</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Over SLA / Jatuh Tempo</th>'
        f'<th style="padding: 8px 10px; border-right: 1px solid rgba(255,255,255,0.2);">Nama Penerima</th>'
        f'<th style="padding: 8px 10px;">Alamat Penerima</th>'
        f'</tr>'
        f'</thead>'
        f'<tbody>{tbody_content}</tbody>'
        f'</table>'
        f'</div>'
    )

    st.markdown(full_card_html, unsafe_allow_html=True)


# --- SIDEBAR: FILTER PARAMETER ---
st.sidebar.header("⚙️ Filter Parameter")

today = datetime.date.today()
tgl_hari_ini = st.sidebar.date_input("Tanggal Jatuh Tempo (Hari Ini)", today)
kc_input = st.sidebar.text_input("KC Tujuan", value="69200")

tgl_h6 = tgl_hari_ini - datetime.timedelta(days=6)

# KARTU ATURAN RAPI DI SIDEBAR
st.sidebar.markdown(
    f"""
    <div style="background-color: #f8fafc; border-left: 3px solid #0284c7; border-radius: 6px; padding: 10px; font-size: 12px; margin-top: 10px; color: #334155;">
        <div style="font-weight: 700; margin-bottom: 4px; color: #0369a1;">📌 Periode Pemantauan:</div>
        <div><b>{tgl_h6.strftime('%d/%m/%Y')}</b> s.d. <b>{tgl_hari_ini.strftime('%d/%m/%Y')}</b></div>
        <div style="margin-top: 6px; font-weight: 700; color: #0369a1;">⚙️ Lingkup Data:</div>
        <div style="line-height: 1.4;">• <b>Hari Ini:</b> Dalam & Luar Kendali</div>
        <div style="line-height: 1.4;">• <b>6 Hari Lalu:</b> Khusus Dalam Kendali (692xx)</div>
        <div style="line-height: 1.4;">• <b>Retur Barang:</b> Hanya jika masih di 692xx</div>
    </div>
    """,
    unsafe_allow_html=True
)

st.sidebar.markdown("<br>", unsafe_allow_html=True)
if st.sidebar.button("🔄 Refresh Data"):
    st.cache_data.clear()
    st.rerun()

# --- AMBIL DATA ---
with st.spinner(f"Memuat data jatuh tempo ({tgl_h6.strftime('%d/%m/%Y')} s.d. {tgl_hari_ini.strftime('%d/%m/%Y')})..."):
    df_raw = fetch_monitoring_data(kc_input, tgl_hari_ini)

# --- HEADER DASHBOARD ---
st.title("📦 Monitoring Kiriman Jatuh Tempo")
st.caption(f"Wilayah KC: **{kc_input}** | Acuan Hari Ini: **{tgl_hari_ini.strftime('%d/%m/%Y')}** (Plus 6 Hari Sebelumnya Dalam Kendali)")

if df_raw.empty:
    st.warning(f"Tidak ada kiriman jatuh tempo untuk KC {kc_input} pada periode ini.")
    st.stop()

# --- FILTER TAMBAHAN (SIDEBAR) ---
st.sidebar.markdown("---")
st.sidebar.subheader("Filter Tampilan")

list_sla = df_raw["Status SLA"].unique().tolist()
sel_sla = st.sidebar.multiselect("Status SLA", options=list_sla, default=list_sla)

list_periode = df_raw["Periode"].unique().tolist()
sel_periode = st.sidebar.multiselect("Periode", options=list_periode, default=list_periode)

list_kendali = df_raw["Kendali"].unique().tolist()
sel_kendali = st.sidebar.multiselect("Area Kendali", options=list_kendali, default=list_kendali)

list_layanan = df_raw["Layanan"].dropna().unique().tolist()
sel_layanan = st.sidebar.multiselect("Layanan", options=list_layanan, default=list_layanan)

list_status = df_raw["Status"].dropna().unique().tolist()
sel_status = st.sidebar.multiselect("Status Terakhir", options=list_status, default=list_status)

df_filtered = df_raw[
    (df_raw["Status SLA"].isin(sel_sla)) &
    (df_raw["Periode"].isin(sel_periode)) &
    (df_raw["Kendali"].isin(sel_kendali)) &
    (df_raw["Layanan"].isin(sel_layanan)) &
    (df_raw["Status"].isin(sel_status))
].copy()

# --- HITUNG METRICS ---
total_item = len(df_filtered)
total_oversla = len(df_filtered[df_filtered["Status SLA"] == "Over SLA"])
total_jatuhtempo = len(df_filtered[df_filtered["Status SLA"] == "Jatuh Tempo"])
total_dalam = len(df_filtered[df_filtered["Kendali"] == "Dalam Kendali"])
total_luar = len(df_filtered[df_filtered["Kendali"] == "Di Luar Kendali"])

# ========================================================
# 5 KARTU METRIK MODERN DENGAN FONT ANGKA BESAR BOLD
# ========================================================
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    render_metric_card(
        title="Total Resi Terpantau",
        value=total_item,
        badge_text="Periode H s.d. H-6",
        badge_bg="#e2e8f0",
        badge_color="#334155",
        card_border="#cbd5e1",
        icon_char="📦"
    )

with c2:
    render_metric_card(
        title="Over SLA",
        value=total_oversla,
        badge_text=f"↑ {total_oversla} lewat deadline",
        badge_bg="#fee2e2",
        badge_color="#b91c1c",
        card_border="#fca5a5",
        icon_char="🚨"
    )

with c3:
    render_metric_card(
        title="Jatuh Tempo Hari Ini",
        value=total_jatuhtempo,
        badge_text=f"↑ {total_jatuhtempo} deadline hari ini",
        badge_bg="#fef3c7",
        badge_color="#b45309",
        card_border="#fcd34d",
        icon_char="⏳"
    )

with c4:
    render_metric_card(
        title="Dalam Kendali (692xx)",
        value=total_dalam,
        badge_text="Di UPT KC/KCP",
        badge_bg="#dcfce7",
        badge_color="#15803d",
        card_border="#86efac",
        icon_char="🟢"
    )

with c5:
    render_metric_card(
        title="Di Luar Kendali",
        value=total_luar,
        badge_text="Luar Wilayah (Hari Ini)",
        badge_bg="#f1f5f9",
        badge_color="#475569",
        card_border="#cbd5e1",
        icon_char="🔴"
    )

st.markdown("<br>", unsafe_allow_html=True)

# --- GRAFIK DALAM KENDALI VS DI LUAR KENDALI (NAVY BLUE) ---
g1, g2 = st.columns(2)
with g1:
    st.subheader("📊 Distribusi Posisi Kiriman")
    kendali_summary = df_filtered["Kendali"].value_counts().reset_index()
    kendali_summary.columns = ["Status Kendali", "Jumlah"]
    st.bar_chart(
        kendali_summary.set_index("Status Kendali"),
        color="#002060"
    )

with g2:
    st.subheader("🏢 Sebaran Kiriman per KC/KCP Terkini")
    loc_summary = df_filtered["KC/KCP"].value_counts().head(10).reset_index()
    loc_summary.columns = ["KC/KCP", "Jumlah"]
    st.bar_chart(
        loc_summary.set_index("KC/KCP"),
        color="#002060"
    )

st.markdown("---")

# ========================================================
# BAGIAN KARTU TABEL PER PENGANTAR (SIAP SCREENSHOT)
# ========================================================
st.subheader("📸 Format Tabel per Petugas Pengantar (Siap Screenshot)")

unique_couriers = [p for p in df_filtered["Petugas Update"].dropna().unique().tolist() if p != "-"]

WARNA_LIST = ["#002060", "#c00000"]

mode_tampilan = st.radio(
    "Pilihan Tampilan:",
    ["Semua Petugas Sekaligus", "Pilih 1 Petugas Tertentu"],
    horizontal=True
)

if mode_tampilan == "Pilih 1 Petugas Tertentu":
    pilih_petugas = st.selectbox("Pilih Petugas yang Ingin Ditampilkan:", unique_couriers)
    if pilih_petugas:
        df_sub = df_filtered[df_filtered["Petugas Update"] == pilih_petugas]
        render_screenshot_card(pilih_petugas, df_sub, header_bg="#002060")
else:
    if not unique_couriers:
        st.info("Tidak ada petugas yang terdaftar di data saat ini.")
    for idx, courier in enumerate(unique_couriers):
        warna_header = WARNA_LIST[idx % 2]
        df_sub = df_filtered[df_filtered["Petugas Update"] == courier]
        render_screenshot_card(courier, df_sub, header_bg=warna_header)

st.markdown("---")

# ========================================================
# TABEL RINCIAN LENGKAP UTAMA
# ========================================================
st.subheader("📋 Daftar Rincian Kiriman (Keseluruhan)")

search_kw = st.text_input("🔍 Cari Resi, Penerima, KC/KCP, atau Petugas:", placeholder="Ketik kata kunci...")
if search_kw:
    kw = search_kw.lower()
    cols_search = ["connote", "Penerima", "Alamat", "KC/KCP", "Petugas Update", "Status SLA"]
    df_filtered = df_filtered[
        df_filtered[cols_search].astype(str).apply(lambda row: row.str.lower().str.contains(kw)).any(axis=1)
    ]

df_filtered = df_filtered.reset_index(drop=True)
df_filtered.insert(0, "nomor", df_filtered.index + 1)

cols_to_render = [
    "nomor",
    "connote",
    "KC/KCP",
    "Petugas Update",
    "Status SLA",
    "Tgl Jatuh Tempo",
    "Tgl Update",
    "Status",
    "Layanan",
    "Penerima",
    "Alamat",
    "First Attempt",
    "Alasan Gagal Antar",
    "Irregularity",
    "Kendali"
]

cols_valid = [c for c in cols_to_render if c in df_filtered.columns]

st.dataframe(
    df_filtered[cols_valid],
    use_container_width=True,
    height=400,
    hide_index=True
)

csv_bytes = df_filtered[cols_valid].to_csv(index=False).encode("utf-8")
st.download_button(
    label="📥 Unduh Data (CSV)",
    data=csv_bytes,
    file_name=f"jatuh_tempo_{kc_input}_{tgl_hari_ini}.csv",
    mime="text/csv"
)