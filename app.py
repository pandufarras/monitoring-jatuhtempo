import base64
import datetime
import io
import json
import re
import urllib.parse
from difflib import SequenceMatcher

from PIL import Image, ImageOps
import pandas as pd
import requests
import streamlit as st
import urllib3

# OCR bersifat OPSIONAL: jika tidak terpasang, algoritma lama tetap jalan
try:
    import pytesseract
    OCR_TERSEDIA = True
except Exception:
    OCR_TERSEDIA = False

# Nonaktifkan warning SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- KONFIGURASI HALAMAN WAJIB PALING ATAS ---
st.set_page_config(
    page_title="Dashboard Operasional KC Sampang",
    page_icon="📮",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Endpoint & Kredensial Elasticsearch Mile App
URL_ES = "https://board.mile.app/elasticsearch/expos.package_connote.pos.*/_search"
HEADERS_ES = {"Content-Type": "application/json", "kbn-xsrf": "true"}

AUTH_USER = st.secrets.get("ES_USER")
AUTH_PASS = st.secrets.get("ES_PASS")
AUTH_ES = (AUTH_USER, AUTH_PASS)


# ========================================================
# MANAJEMEN AUTENTIKASI & SESSION COOKIE LINTAS TAB
import hmac

# ========================================================
# MANAJEMEN AUTENTIKASI LINTAS TAB (BAWAAN STREAMLIT QUERY PARAMS)
# ========================================================
def check_login() -> bool:
    cfg_auth = st.secrets.get("credentials", {})
    valid_user = cfg_auth.get("username")
    valid_pass = cfg_auth.get("password")

    if not valid_user or not valid_pass:
        st.error("Kredensial login belum disetel di Secrets Streamlit.")
        st.stop()

    # Buat token sesi sederhana berdasarkan hash kredensial rahasia
    expected_token = hmac.new(
        key=valid_pass.encode(),
        msg=valid_user.encode(),
        digestmod="sha256"
    ).hexdigest()[:16]

    # 1. Cek sesi aktif di session_state
    if st.session_state.get("authenticated", False):
        return True

    # 2. Cek token di URL parameter (agar saat buka tab baru tetap otomatis login)
    current_token = st.query_params.get("session_auth", "")
    if current_token == expected_token:
        st.session_state["authenticated"] = True
        return True

    # 3. Form Login jika belum ada sesi
    _, col_form, _ = st.columns([1, 1.5, 1])
    with col_form:
        st.markdown("<br><br>", unsafe_allow_html=True)
        st.subheader("🔒 Login KC Sampang")

        with st.form("form_login"):
            username_input = st.text_input("Username")
            password_input = st.text_input("Password", type="password")
            submit = st.form_submit_button("Masuk", use_container_width=True)

            if submit:
                if username_input == valid_user and password_input == valid_pass:
                    st.session_state["authenticated"] = True
                    # Tempel token ke query URL agar terbawa ke tab baru
                    st.query_params["session_auth"] = expected_token
                    st.success("Login berhasil!")
                    st.rerun()
                else:
                    st.error("Username atau password salah.")

    return False

# Jalankan pencegat login sebelum dashboard dimuat
if not check_login():
    st.stop()
# ========================================================
# 1A. LAPISAN OCR: BACA TEKS PADA GAMBAR (tahan blur, tint, rotasi)
# ========================================================
KW_KUAT = [
    "NIK", "PROVINSI", "KEWARGANEGARAAN", "GOL DARAH", "STATUS PERKAWINAN",
    "BERLAKU HINGGA", "JENIS KELAMIN", "TEMPAT TGL LAHIR", "KARTU KELUARGA",
    "KEPALA KELUARGA", "NO KK", "NAMA LENGKAP", "SURAT IZIN MENGEMUDI",
]
KW_PENDUKUNG = [
    "KABUPATEN", "JAWA TIMUR", "AGAMA", "PEKERJAAN", "KEL DESA", "KECAMATAN",
    "ISLAM", "WNI", "KAWIN", "WIRASWASTA", "LAKI LAKI", "PEREMPUAN",
]


def _norm_text(t: str) -> str:
    t = t.upper()
    t = re.sub(r"[^A-Z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _kw_cocok(kw: str, words: list) -> bool:
    parts = kw.split()
    n = len(parts)
    if n == 1 and len(kw) <= 3:
        return kw in words
    target = " ".join(parts)
    for i in range(len(words) - n + 1):
        cand = " ".join(words[i:i + n])
        if cand == target or SequenceMatcher(None, cand, target).ratio() >= 0.82:
            return True
    return False


def _skor_teks_identitas(teks: str) -> tuple:
    norm = _norm_text(teks)
    words = norm.split()
    kuat = sum(1 for k in KW_KUAT if _kw_cocok(k, words))
    pend = sum(1 for k in KW_PENDUKUNG if _kw_cocok(k, words))
    digits_only = re.sub(r"(?<=\d) (?=\d)", "", norm)
    ada_nik = bool(re.search(r"\b\d{14,17}\b", digits_only))
    return kuat, pend, ada_nik


def ocr_identity_check(img: Image.Image) -> tuple:
    if not OCR_TERSEDIA:
        return (False, "OCR tidak tersedia")
    try:
        gray = ImageOps.grayscale(img)
        w, h = gray.size
        scale = 1100 / max(w, 1)
        if scale != 1:
            gray = gray.resize((1100, max(1, int(h * scale))), Image.LANCZOS)
        gray = ImageOps.autocontrast(gray, cutoff=2)

        for angle in (0, 270, 90, 180):
            g = gray if angle == 0 else gray.rotate(angle, expand=True)
            teks = pytesseract.image_to_string(g, config="--psm 11", timeout=10)
            kuat, pend, nik = _skor_teks_identitas(teks)
            if (kuat >= 1 and kuat + pend >= 2) or kuat >= 2 or (nik and (kuat + pend) >= 1):
                return (True, "Teks identitas terbaca (OCR)")
        return (False, "Teks KTP/KK tidak terbaca")
    except Exception as e:
        return (False, f"OCR gagal: {str(e)[:20]}")


# ========================================================
# 1B. ALGORITMA HEURISTIK CITRA DOKUMEN IDENTITAS (TAHAP 1)
# ========================================================
def inspect_image_is_identity_document(img_bytes: bytes) -> tuple:
    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w_orig, h_orig = img.size
        aspect_ratio = h_orig / max(w_orig, 1)

        img_thumb = img.resize((150, 150))
        pixels = list(img_thumb.getdata())
        total_p = len(pixels)

        ktp_cyan = 0
        dark_text = 0
        paper_doc = 0
        wa_bubble = 0

        for r, g, b in pixels:
            diff_max = max(abs(r - g), abs(g - b), abs(r - b))

            if r < 120 and g < 120 and b < 120 and diff_max <= 30:
                dark_text += 1
            elif 80 <= r <= 255 and 80 <= g <= 255 and 70 <= b <= 255 and diff_max <= 40:
                paper_doc += 1
            elif b >= 50 and b > r * 1.05 and (b >= g or abs(b - g) <= 35):
                ktp_cyan += 1

            if 5 <= r <= 50 and 65 <= g <= 135 and 45 <= b <= 115 and g > r * 1.4 and g > b:
                wa_bubble += 1
            elif 190 <= r <= 235 and 230 <= g <= 255 and 190 <= b <= 235 and g > r + 5 and g > b + 5:
                wa_bubble += 1

        pct_wa = (wa_bubble / total_p) * 100
        pct_cyan = (ktp_cyan / total_p) * 100
        pct_paper = (paper_doc / total_p) * 100
        pct_text = (dark_text / total_p) * 100

        if aspect_ratio >= 1.50 and pct_wa >= 0.5 and pct_cyan < 2.0:
            return (False, "Foto terdeteksi screenshot chat WA")

        if pct_paper >= 15.0 and pct_text >= 2.5:
            return (True, "")
        if pct_text >= 5.0:
            return (True, "")
        if pct_cyan >= 1.0:
            return (True, "")

        if OCR_TERSEDIA:
            ok, _alasan = ocr_identity_check(img)
            if ok:
                return (True, "")

        if not OCR_TERSEDIA and pct_paper >= 20.0 and pct_text >= 1.5:
            return (True, "")

        return (False, "Bukan pola dokumen KTP/KK/Identitas")

    except Exception as e:
        return (False, f"Gagal analisa: {str(e)[:25]}")


def evaluate_two_photos(f1_url: str, f2_url: str) -> tuple:
    if not f1_url or not f2_url:
        return ("INVALID", "Foto identitas tidak ada (kurang foto)")

    if f1_url.strip() == f2_url.strip():
        return ("INVALID", "Foto duplikat (kedua foto identik)")

    try:
        r2 = requests.get(f2_url, timeout=7, verify=False)
        if r2.status_code == 200:
            is_valid_2, alasan_2 = inspect_image_is_identity_document(r2.content)
            if is_valid_2:
                return ("VALID", "")
            return ("INVALID", alasan_2)
        return ("INVALID", "Gagal memuat foto dari server")
    except Exception:
        return ("INVALID", "Foto identitas tidak dapat diverifikasi")


@st.cache_data(show_spinner=False)
def evaluate_connote_photos_cached(connote_id: str, f1_url: str, f2_url: str) -> tuple:
    return evaluate_two_photos(f1_url, f2_url)


# ========================================================
# 2. VERIFIKASI DOKUMEN DENGAN GEMINI AI (TAHAP 2)
# ========================================================
# 2. VERIFIKASI DOKUMEN DENGAN GEMINI AI (TAHAP 2)
# ========================================================
@st.cache_data(ttl=1800)
def get_valid_gemini_endpoint(api_key: str) -> str:
    """Mengambil model aktif langsung dari akun Google AI Studio."""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            daftar = res.json().get("models", [])
            # Cari model yang mendukung generateContent
            nama_tersedia = [
                m.get("name") for m in daftar 
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            # Prioritaskan varian flash
            for prioritas in [
                "models/gemini-2.0-flash",
                "models/gemini-2.0-flash-exp",
                "models/gemini-1.5-flash",
                "models/gemini-1.5-flash-latest",
                "models/gemini-1.5-flash-8b",
                "models/gemini-1.5-pro"
            ]:
                if prioritas in nama_tersedia:
                    return f"https://generativelanguage.googleapis.com/v1beta/{prioritas}:generateContent?key={api_key}"
            
            # Jika tidak ada yang cocok di atas, ambil model pertama yang mendukung generateContent
            if nama_tersedia:
                return f"https://generativelanguage.googleapis.com/v1beta/{nama_tersedia[0]}:generateContent?key={api_key}"
    except Exception:
        pass
    # Fallback default
    return f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"


def analyze_document_with_gemini(img_url: str) -> tuple:
    api_key = str(st.secrets.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        return (False, "API Key belum disetel di Secrets")

    try:
        # 1. Unduh dan perkecil ukuran gambar
        resp_img = requests.get(img_url, timeout=12, verify=False)
        if resp_img.status_code != 200:
            return (False, "Gagal mengunduh gambar")

        img = Image.open(io.BytesIO(resp_img.content)).convert("RGB")
        img.thumbnail((700, 700), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        img_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        prompt_text = (
            "Periksa apakah gambar ini adalah dokumen identitas resmi penduduk "
            "(e-KTP fisik, Kartu Keluarga/KK, SIM, atau fotokopi KTP/KK yang terbaca). "
            "Jika berupa foto orang/wajah saja, foto rumah, teras, pagar, plang kantor, jalan, amplop tanpa KTP, atau screenshot chat, "
            "maka BUKAN dokumen identitas.\n"
            "Wajib jawab HANYA format JSON persis: "
            "{\"valid\": true, \"alasan\": \"KTP/KK sah\"} atau "
            "{\"valid\": false, \"alasan\": \"penjelasan ringkas maks 6 kata\"}"
        )

        headers = {"Content-Type": "application/json"}
        payload = {
            "contents": [{
                "parts": [
                    {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                    {"text": prompt_text}
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json"
            }
        }

        # 2. Pakai endpoint resmi gemini-1.5-flash (pasti aktif di semua akun)
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
        resp = requests.post(api_url, headers=headers, json=payload, timeout=25)

        if resp.status_code != 200:
            err_msg = resp.json().get("error", {}).get("message", f"HTTP {resp.status_code}")
            return (False, f"API Error: {err_msg[:35]}")

        raw_text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        clean_text = re.sub(r"^```json\s*|\s*```$", "", raw_text.strip())
        data = json.loads(clean_text)

        is_v = bool(data.get("valid", False))
        alasan = data.get("alasan", "Bukan dokumen KTP/KK" if not is_v else "")
        return (is_v, alasan)

    except Exception as e:
        return (False, f"Gagal AI: {str(e)[:30]}")
# ========================================================
# FUNGSI BANTUAN OPERASIONAL
# ========================================================
def generate_pid_url(connote_str: str) -> str:
    if not connote_str or connote_str == "-":
        return ""
    b64_val = base64.b64encode(str(connote_str).strip().encode("utf-8")).decode("utf-8")
    param_val = urllib.parse.quote(b64_val)
    return f"https://pid.posindonesia.co.id/lacak/admin/detail_lacak_banyak.php?id={param_val}"


def format_tgl_update(raw_dt) -> str:
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
    if not isinstance(src, dict):
        return "-"
    curr_loc = src.get("currentLocation") or {}
    custom = src.get("custom_field") or {}
    pod = src.get("pod") or {}
    connote = src.get("connote") or {}

    petugas = curr_loc.get("full_name")
    if petugas and str(petugas).strip() and str(petugas).strip() not in ["__missing__", "-"]:
        return str(petugas).strip()

    petugas = (
        curr_loc.get("user_name")
        or custom.get("first_attempt_courier_name")
        or custom.get("courier_name")
        or custom.get("updated_by_name")
        or pod.get("courier_name")
        or connote.get("user_name")
    )
    return str(petugas).strip() if petugas else "-"


def extract_coordinate_gmaps(src: dict) -> tuple:
    if not isinstance(src, dict):
        return ("-", "")
    pod = src.get("pod") or {}
    coord = pod.get("coordinate") or {}
    lat, lon = None, None

    if isinstance(coord, dict):
        lat = coord.get("lat") or coord.get("latitude")
        lon = coord.get("lon") or coord.get("longitude")
    elif isinstance(coord, str) and "," in coord:
        parts = coord.split(",")
        lat, lon = parts[0].strip(), parts[1].strip()

    if lat is not None and lon is not None:
        gmaps_link = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
        coord_text = f"{lat:.5f}, {lon:.5f}" if isinstance(lat, float) else f"{lat}, {lon}"
        return (coord_text, gmaps_link)
    return ("-", "")


def extract_all_photos(src: dict) -> tuple:
    if not isinstance(src, dict):
        return ("", "")
    
    pod = src.get("pod") or {}
    custom = src.get("custom_field") or {}
    connote = src.get("connote") or {}

    kandidat = []
    
    def cari_url(obj):
        if isinstance(obj, str):
            obj_l = obj.lower()
            if ("http" in obj or "apistorage" in obj) and any(e in obj_l for e in [".jpg", ".jpeg", ".png"]):
                if "signature" not in obj_l and "ttd" not in obj_l:
                    url_clean = obj.strip()
                    if url_clean not in kandidat:
                        kandidat.append(url_clean)
        elif isinstance(obj, dict):
            for v in obj.values(): cari_url(v)
        elif isinstance(obj, list):
            for v in obj: cari_url(v)

    f_1 = pod.get("photo") or pod.get("photo1") or ""
    f_2 = pod.get("photo2") or pod.get("photo_ktp") or ""
    f_3 = pod.get("photo3") or pod.get("photo_identitas") or ""

    cari_url(pod)
    cari_url(custom)
    cari_url(connote)

    kandidat_bersih = [u for u in kandidat if "signature" not in u.lower() and "ttd" not in u.lower()]

    foto_orang = ""
    foto_ktp = ""

    if f_1 and f_1 in kandidat_bersih:
        foto_orang = f_1
    elif len(kandidat_bersih) > 0:
        foto_orang = kandidat_bersih[0]

    if f_3 and f_3 != foto_orang and f_3 in kandidat_bersih:
        foto_ktp = f_3
    elif f_2 and f_2 != foto_orang and f_2 in kandidat_bersih:
        foto_ktp = f_2
    else:
        for u in kandidat_bersih:
            if u != foto_orang:
                foto_ktp = u
                break

    if not foto_ktp and foto_orang:
        foto_ktp = foto_orang

    return (foto_orang, foto_ktp)


def render_metric_card(title: str, value: int, badge_text: str = "", badge_bg: str = "#e2e8f0", badge_color: str = "#334155", card_border: str = "#e2e8f0", icon_char: str = ""):
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
        <div>{badge_html}</div>
    </div>
    """
    st.markdown(card_html, unsafe_allow_html=True)


# ========================================================
# NAVIGASI MENU SIDEBAR & LOGOUT
# ========================================================
st.sidebar.title("🎛️ Navigasi Menu")

if st.sidebar.button("🚪 Keluar (Logout)"):
    st.session_state["authenticated"] = False
    if "session_auth" in st.query_params:
        del st.query_params["session_auth"]
    st.rerun()

st.sidebar.markdown("---")
menu_pilihan = st.sidebar.radio(
    "Pilih Dashboard:",
    ["📦 Monitoring Jatuh Tempo", "⚖️ Uji Petik Mahkamah Agung (PA/PN)"],
    index=1
)
st.sidebar.markdown("---")


# ==============================================================================
# MENU 1: MONITORING JATUH TEMPO
# ==============================================================================
if menu_pilihan == "📦 Monitoring Jatuh Tempo":
    st.title("📦 Monitoring Kiriman Jatuh Tempo")

    today = datetime.date.today()
    tgl_hari_ini = st.sidebar.date_input("Tanggal Jatuh Tempo (Hari Ini)", today)
    kc_input = st.sidebar.text_input("KC Tujuan", value="69200")
    tgl_h6 = tgl_hari_ini - datetime.timedelta(days=6)

    st.sidebar.markdown(
        f"""
        <div style="background-color: #f8fafc; border-left: 3px solid #0284c7; border-radius: 6px; padding: 10px; font-size: 12px; margin-top: 10px; color: #334155;">
            <div style="font-weight: 700; margin-bottom: 4px; color: #0369a1;">📌 Periode Pemantauan:</div>
            <div><b>{tgl_h6.strftime('%d/%m/%Y')}</b> s.d. <b>{tgl_hari_ini.strftime('%d/%m/%Y')}</b></div>
        </div>
        """,
        unsafe_allow_html=True
    )

    if st.sidebar.button("🔄 Refresh Data"):
        st.cache_data.clear()
        st.rerun()

    def build_payload_jt(kc_code: str, start_date: datetime.date, end_date: datetime.date) -> dict:
        prev_start = start_date - datetime.timedelta(days=1)
        gte_utc = f"{prev_start.strftime('%Y-%m-%d')}T17:00:00.000Z"
        lte_utc = f"{end_date.strftime('%Y-%m-%d')}T16:59:59.999Z"

        return {
            "size": 5000,
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
                        {"match_phrase": {"custom_field.destination_kprk.keyword": {"query": str(kc_code)}}},
                        {"range": {"custom_field.final_swp_date_new": {"format": "strict_date_optional_time", "gte": gte_utc, "lte": lte_utc}}},
                    ],
                    "must_not": [
                        {"bool": {"should": [{"match_phrase": {"connote.connote_state.keyword": "DELIVERED"}}, {"match_phrase": {"connote.connote_state.keyword": "DELIVERED (RETURN DELIVERY)"}}], "minimum_should_match": 1}},
                        {"bool": {"should": [{"match_phrase": {"connote.connote_state.keyword": "CANCEL"}}, {"match_phrase": {"connote.connote_state.keyword": "PENDING"}}], "minimum_should_match": 1}},
                        {"match_phrase": {"connote.location_name.keyword": {"query": "AGP TESTING LOCATION"}}},
                        {"match_phrase": {"connote.connote_service.keyword": {"query": "LNINCOMING"}}},
                    ],
                }
            },
        }

    @st.cache_data(ttl=120)
    def fetch_data_jt(kc_code: str, target_date: datetime.date) -> pd.DataFrame:
        start_date = target_date - datetime.timedelta(days=6)
        payload = build_payload_jt(kc_code, start_date, target_date)
        try:
            resp = requests.post(URL_ES, headers=HEADERS_ES, auth=AUTH_ES, json=payload, verify=False, timeout=35)
        except Exception as e:
            st.error(f"Gagal koneksi ES: {e}")
            return pd.DataFrame()

        hits = resp.json().get("hits", {}).get("hits", []) if resp.status_code == 200 else []
        rows = []
        for h in hits:
            src = h.get("_source", {})
            connote = src.get("connote", {})
            custom = src.get("custom_field", {})
            curr_loc = src.get("currentLocation", {})

            loc_name = curr_loc.get("name") or "-"
            loc_code = str(curr_loc.get("code") or "")
            is_dalam = loc_code.startswith("692") or ("692" in loc_name)

            raw_swp = custom.get("final_swp_date_new")
            jt_date = extract_jatuh_tempo_date_wib(raw_swp)

            if jt_date and jt_date < target_date and not is_dalam:
                continue

            c_code = str(connote.get("connote_code") or h.get("_id")).strip()
            rows.append({
                "connote": c_code,
                "url_lacak": generate_pid_url(c_code),
                "kantor update": loc_name,
                "Tgl Update": format_tgl_update(connote.get("updated_at") or src.get("updated_at")),
                "Petugas Update": extract_petugas(src),
                "Status": connote.get("connote_state") or "-",
                "Layanan": connote.get("connote_service") or "-",
                "Status SLA": "Over SLA" if (jt_date and jt_date < target_date) else "Jatuh Tempo",
                "Penerima": connote.get("connote_receiver_name") or "-",
                "Alamat": connote.get("connote_receiver_address") or "-",
                "Kendali": "Dalam Kendali" if is_dalam else "Di Luar Kendali",
            })
        return pd.DataFrame(rows)

    with st.spinner("Memuat data jatuh tempo..."):
        df_jt = fetch_data_jt(kc_input, tgl_hari_ini)

    st.caption(f"Wilayah KC: **{kc_input}** | Periode: **{tgl_h6.strftime('%d/%m/%Y')}** s.d. **{tgl_hari_ini.strftime('%d/%m/%Y')}**")

    if df_jt.empty:
        st.warning("Tidak ada kiriman jatuh tempo pada periode ini.")
        st.stop()

    t_item = len(df_jt)
    t_over = len(df_jt[df_jt["Status SLA"] == "Over SLA"])
    t_jt_today = len(df_jt[df_jt["Status SLA"] == "Jatuh Tempo"])
    t_dalam = len(df_jt[df_jt["Kendali"] == "Dalam Kendali"])
    t_luar = len(df_jt[df_jt["Kendali"] == "Di Luar Kendali"])

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1: render_metric_card("Total Resi Terpantau", t_item, "Periode H s.d. H-6", "#e2e8f0", "#334155", "#cbd5e1", "📦")
    with c2: render_metric_card("Over SLA", t_over, f"↑ {t_over} lewat deadline", "#fee2e2", "#b91c1c", "#fca5a5", "🚨")
    with c3: render_metric_card("Jatuh Tempo Hari Ini", t_jt_today, f"↑ {t_jt_today} deadline hari ini", "#fef3c7", "#b45309", "#fcd34d", "⏳")
    with c4: render_metric_card("Dalam Kendali (692xx)", t_dalam, "Di UPT KC/KCP", "#dcfce7", "#15803d", "#86efac", "🟢")
    with c5: render_metric_card("Di Luar Kendali", t_luar, "Luar Wilayah", "#f1f5f9", "#475569", "#cbd5e1", "🔴")

    st.markdown("---")
    st.subheader("📸 Format Tabel per Petugas Pengantar (Siap Screenshot)")

    couriers = [p for p in df_jt["Petugas Update"].dropna().unique() if p != "-"]
    colors = ["#002060", "#c00000"]

    for idx, courier in enumerate(couriers):
        df_sub = df_jt[df_jt["Petugas Update"] == courier]
        tbody_rows = []
        for _, r in df_sub.iterrows():
            sla_c = "#b30000" if r['Status SLA'] == "Over SLA" else "#d97706"
            tbody_rows.append(
                f"<tr style='border-bottom: 1px solid #ddd; font-size: 13px;'>"
                f"<td style='padding: 8px 10px;'><a href='{r['url_lacak']}' target='_blank' style='color:#002060; font-weight:bold; text-decoration:underline;'>{r['connote']}</a></td>"
                f"<td style='padding: 8px 10px;'>{r['kantor update']}</td>"
                f"<td style='padding: 8px 10px; text-align:center;'>{r['Tgl Update']}</td>"
                f"<td style='padding: 8px 10px;'>{r['Petugas Update']}</td>"
                f"<td style='padding: 8px 10px; text-align:center; font-weight:600;'>{r['Status']}</td>"
                f"<td style='padding: 8px 10px; text-align:center; font-weight:bold;'>{r['Layanan']}</td>"
                f"<td style='padding: 8px 10px; text-align:center; font-weight:bold; color:{sla_c};'>{r['Status SLA']}</td>"
                f"<td style='padding: 8px 10px;'>{r['Penerima']}</td>"
                f"<td style='padding: 8px 10px;'>{r['Alamat']}</td>"
                f"</tr>"
            )

        st.markdown(
            f"<div style='margin-bottom: 25px; border-radius: 6px; overflow: hidden; border: 1px solid #94a3b8; box-shadow: 0 1px 4px rgba(0,0,0,0.1);'>"
            f"<div style='background-color: #ffffff; text-align: center; padding: 8px; font-size: 17px; font-weight: 900; border-bottom: 2px solid #111;'>{courier}</div>"
            f"<table style='width: 100%; border-collapse: collapse; font-family: sans-serif;'>"
            f"<thead><tr style='background-color: {colors[idx % 2]}; color: #ffffff; font-size: 13px; text-align: center;'>"
            f"<th style='padding: 8px;'>Nomor Resi</th><th>Kantor Update</th><th>Tgl Update</th><th>Petugas Update</th><th>Status</th><th>Produk</th><th>Status SLA</th><th>Penerima</th><th>Alamat</th>"
            f"</tr></thead>"
            f"<tbody>{''.join(tbody_rows)}</tbody>"
            f"</table>"
            f"</div>",
            unsafe_allow_html=True
        )


# ==============================================================================
# MENU 2: UJI PETIK MAHKAMAH AGUNG (PA / PN)
# ==============================================================================
elif menu_pilihan == "⚖️ Uji Petik Mahkamah Agung (PA/PN)":
    st.title("⚖️ Laporan Uji Petik Kiriman MA (PA/PN) KC Sampang")
    st.caption("Monitoring Kiriman Surat Tercatat MA (LNMAPAG05692A & LNMAPN05692A)")

    today = datetime.date.today()
    default_tgl = today - datetime.timedelta(days=2)
    
    d_start = st.sidebar.date_input("Dari Tanggal", default_tgl)
    d_end = st.sidebar.date_input("Sampai Tanggal", default_tgl)

    filter_status = st.sidebar.radio(
        "Filter Status Kiriman:",
        ["Semua Status", "⚠️ Hanya INVALID (Perlu Cek)", "✅ Hanya VALID"],
        index=0
    )

    if st.sidebar.button("🔄 Refresh Data MA"):
        st.cache_data.clear()
        if "audit_data" in st.session_state:
            del st.session_state["audit_data"]
        if "manual_overrides" in st.session_state:
            del st.session_state["manual_overrides"]
        st.rerun()

    def build_payload_ma(start_date: datetime.date, end_date: datetime.date) -> dict:
        gte_utc = f"{start_date.strftime('%Y-%m-%d')}T00:00:00.000Z"
        lte_utc = f"{end_date.strftime('%Y-%m-%d')}T23:59:59.999Z"

        return {
            "size": 2000,
            "_source": {"excludes": ["pod.reason_onprocess*"]},
            "query": {
                "bool": {
                    "filter": [
                        {"match_phrase": {"connote.connote_state.keyword": "DELIVERED"}},
                        {"match_phrase": {"custom_field.destination_reg_new.keyword": "5"}},
                        {"match_phrase": {"custom_field.destination_kprk.keyword": "69200"}},
                        {
                            "bool": {
                                "should": [
                                    {"match_phrase": {"customer_code.keyword": "LNMAPAG05692A"}},
                                    {"match_phrase": {"customer_code.keyword": "LNMAPN05692A"}},
                                ],
                                "minimum_should_match": 1,
                            }
                        },
                        {"range": {"created_at": {"format": "strict_date_optional_time", "gte": gte_utc, "lte": lte_utc}}},
                    ],
                    "must_not": [
                        {"match_phrase": {"connote.connote_service.keyword": "LNINCOMING"}},
                        {"bool": {"should": [{"match_phrase": {"connote.connote_state.keyword": "CANCEL"}}, {"match_phrase": {"connote.connote_state.keyword": "PENDING"}}], "minimum_should_match": 1}},
                        {"match_phrase": {"location_data_created.location_name.keyword": "AGP TESTING LOCATION"}},
                    ],
                }
            },
        }

    @st.cache_data(ttl=600)
    def fetch_data_ma(start_date: datetime.date, end_date: datetime.date) -> pd.DataFrame:
        payload = build_payload_ma(start_date, end_date)
        try:
            resp = requests.post(URL_ES, headers=HEADERS_ES, auth=AUTH_ES, json=payload, verify=False, timeout=20)
            if resp.status_code != 200:
                st.error(f"Error dari server Elasticsearch ({resp.status_code}): {resp.text[:100]}")
                return pd.DataFrame()
        except Exception as e:
            st.error(f"Gagal koneksi ES: {e}")
            return pd.DataFrame()
        
        hits = resp.json().get("hits", {}).get("hits", [])
        rows = []
        for h in hits:
            src = h.get("_source", {})
            connote = src.get("connote", {})
            curr_loc = src.get("currentLocation", {})
            pod = src.get("pod", {})

            teks_all = " ".join([
                str(connote.get("connote_receiver_name") or ""),
                str(connote.get("connote_receiver_address") or ""),
                str(pod.get("receiver_name") or ""),
            ]).upper()
            if any(k in teks_all for k in ["JAKSA", "KEJARI", "TAHANAN", "RUTAN", "PERTANAHAN", "BPN", "AGRARIA"]):
                continue

            c_code = str(connote.get("connote_code") or h.get("_id")).strip()
            loc_name = curr_loc.get("name") or "-"
            petugas = extract_petugas(src)
            c_txt, m_url = extract_coordinate_gmaps(src)
            f_orang, f_ktp = extract_all_photos(src)

            stat_inv, ket_inv = evaluate_connote_photos_cached(c_code, f_orang, f_ktp)

            state = connote.get("connote_state") or "-"
            rec = pod.get("receiver_name") or connote.get("connote_receiver_name") or ""
            rel = pod.get("relation") or ""
            status_kiriman = f"{state} oleh {rec}" + (f" ({rel})" if rel else "")

            rows.append({
                "connote": c_code,
                "url_lacak": generate_pid_url(c_code),
                "kantor update": loc_name,
                "status kiriman": status_kiriman,
                "petugas update": petugas,
                "nama penerima": connote.get("connote_receiver_name") or "-",
                "alamat penerima": connote.get("connote_receiver_address") or "-",
                "koordinat": c_txt,
                "url_maps": m_url,
                "foto_orang": f_orang,
                "foto_ktp": f_ktp,
                "Hasil Investigasi": stat_inv,
                "Penjelasan Invalid": ket_inv,
            })
        return pd.DataFrame(rows)

    with st.spinner("Memuat data uji petik MA & menganalisis bukti foto..."):
        df_raw_ma = fetch_data_ma(d_start, d_end)

    if df_raw_ma.empty:
        st.warning(f"Belum ditemukan kiriman MA untuk rentang {d_start.strftime('%d/%m/%Y')} s.d. {d_end.strftime('%d/%m/%Y')}.")
        st.stop()

    if "manual_overrides" not in st.session_state:
        st.session_state["manual_overrides"] = {}

    if "audit_data" not in st.session_state:
        df_init = df_raw_ma.copy()

        df_init["pengantar_label"] = df_init.apply(lambda r: f"{r['petugas update']} ( {r['kantor update']} )", axis=1)
        target_nama_gabung = "Moh Iqbal Syahputra ( KCP KETAPANG SAMPANG 69261 )"
        mask_rudy = df_init["pengantar_label"].str.contains("Rudy Ermawanto", case=False, na=False) & \
                    df_init["pengantar_label"].str.contains("69261", case=False, na=False)
        df_init.loc[mask_rudy, "pengantar_label"] = target_nama_gabung
        df_init.loc[mask_rudy, "petugas update"] = "Moh Iqbal Syahputra"

        for connote_key, val in st.session_state["manual_overrides"].items():
            matched = df_init[df_init["connote"] == connote_key].index
            if len(matched) > 0:
                df_init.loc[matched, "Hasil Investigasi"] = val["status"]
                df_init.loc[matched, "Penjelasan Invalid"] = val["ket"]
        st.session_state["audit_data"] = df_init

    df_ma = st.session_state["audit_data"]

    # --- KPI METRIK KESELURUHAN ---
    total_ma = len(df_ma)
    total_valid = len(df_ma[df_ma["Hasil Investigasi"] == "VALID"])
    total_invalid = len(df_ma[df_ma["Hasil Investigasi"] == "INVALID"])
    pct_v = round((total_valid / total_ma * 100), 1) if total_ma > 0 else 0
    pct_i = round((total_invalid / total_ma * 100), 1) if total_ma > 0 else 0

    m1, m2, m3 = st.columns(3)
    with m1: render_metric_card("Total Resi", total_ma, f"Periode {d_start.strftime('%d/%m')} - {d_end.strftime('%d/%m/%Y')}", "#e2e8f0", "#334155", "#cbd5e1", "📦")
    with m2: render_metric_card("✅ Antaran Valid", total_valid, f"↑ {pct_v}% Kepatuhan", "#dcfce7", "#15803d", "#86efac", "✅")
    with m3: render_metric_card("⚠️ Antaran Invalid", total_invalid, f"↑ {pct_i}% Perlu Pembinaan", "#fee2e2", "#b91c1c", "#fca5a5", "⚠️")

    st.markdown("<br>", unsafe_allow_html=True)
    st.subheader("📊 Persentase Kepatuhan Antaran per Petugas")

    summary_rows = []
    for p_name, group in df_ma.groupby("pengantar_label"):
        t_p = int(len(group))
        v_p = int(len(group[group["Hasil Investigasi"] == "VALID"]))
        i_p = int(len(group[group["Hasil Investigasi"] == "INVALID"]))
        m = re.search(r'\b(\d{5})\b', str(group["kantor update"].iloc[0]))
        nopend_num = int(m.group(1)) if m else 99999

        summary_rows.append({
            "Petugas & Kantor": p_name,
            "nopend": nopend_num,
            "Total Kiriman": t_p,
            "Valid": v_p,
            "Invalid": i_p,
            "% Valid": f"{round(v_p / t_p * 100, 1)}%",
            "% Invalid": f"{round(i_p / t_p * 100, 1)}%",
            "val_num": round(v_p / t_p * 100, 1),
        })

    df_summary = pd.DataFrame(summary_rows).sort_values(
        by=["Invalid", "Total Kiriman"],
        ascending=[False, False]
    ).reset_index(drop=True)

    summary_tr = []
    for _, r in df_summary.iterrows():
        b_color = "#16a34a" if r["val_num"] >= 90 else ("#d97706" if r["val_num"] >= 70 else "#dc2626")
        bg_row = "#fef2f2" if r["val_num"] < 100.0 else "#ffffff"

        summary_tr.append(
            f"<tr style='border-bottom: 1px solid #fecaca; background-color: {bg_row}; font-size: 13px;'>"
            f"<td style='padding: 8px 12px; font-weight: 700;'>{r['Petugas & Kantor']}</td>"
            f"<td style='padding: 8px; text-align: center; font-weight: 600;'>{r['Total Kiriman']}</td>"
            f"<td style='padding: 8px; text-align: center; color: #15803d; font-weight: 700;'>{r['Valid']}</td>"
            f"<td style='padding: 8px; text-align: center; color: #b91c1c; font-weight: 700;'>{r['Invalid']}</td>"
            f"<td style='padding: 8px; text-align: center; font-weight: 800; color: #15803d;'>{r['% Valid']}</td>"
            f"<td style='padding: 8px; text-align: center; font-weight: 800; color: #b91c1c;'>{r['% Invalid']}</td>"
            f"<td style='padding: 8px; min-width: 130px;'><div style='background-color:#e2e8f0; border-radius:999px; height:8px; width:100%;'><div style='background-color:{b_color}; width:{r['val_num']}%; height:100%; border-radius:999px;'></div></div></td>"
            f"</tr>"
        )

    st.markdown(
        f"<div style='overflow-x: auto; border: 1px solid #cbd5e1; border-radius: 8px; margin-bottom: 25px;'>"
        f"<table style='width: 100%; border-collapse: collapse; font-family: sans-serif;'>"
        f"<thead><tr style='background-color: #f1f5f9; color: #334155; font-size: 13px; text-align: center; border-bottom: 2px solid #cbd5e1;'>"
        f"<th style='padding: 9px 12px; text-align: left;'>Nama Petugas (Kantor)</th><th>Total Kiriman</th><th>Valid</th><th>Invalid</th><th>% Valid</th><th>% Invalid</th><th style='text-align: left;'>Tingkat Kepatuhan</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(summary_tr)}</tbody>"
        f"</table>"
        f"</div>",
        unsafe_allow_html=True
    )

    st.markdown("---")
    st.subheader("📸 Format Tabel Uji Petik per Petugas Pengantar (Siap Screenshot)")

    def render_tabel_kartu_ma(header_label: str, group_df: pd.DataFrame, header_bg: str = "#002060"):
        table_rows = []
        for idx, (_, row) in enumerate(group_df.iterrows(), start=1):
            r_link = f"<a href='{row['url_lacak']}' target='_blank' style='color:#002060; font-weight:bold; text-decoration:underline;'>{row['connote']}</a>" if row['url_lacak'] else row['connote']
            coord_link = f"<a href='{row['url_maps']}' target='_blank' style='color:#16a34a; font-weight:bold; text-decoration:underline;'>📍 {row['koordinat']}</a>" if row['url_maps'] else "-"
            img_orang = f"<a href='{row['foto_orang']}' target='_blank'><img src='{row['foto_orang']}' style='width: 105px; height: 115px; object-fit: cover; border-radius: 6px; border: 1.5px solid #cbd5e1;'></a>" if row['foto_orang'] else "<span style='color:#94a3b8; font-size:11px;'>Tidak ada foto</span>"
            img_ktp = f"<a href='{row['foto_ktp']}' target='_blank'><img src='{row['foto_ktp']}' style='width: 155px; height: 105px; object-fit: cover; border-radius: 6px; border: 1.5px solid #cbd5e1;'></a>" if row['foto_ktp'] else "<span style='color:#dc2626; font-size:11px; font-weight:bold;'>Tidak ada KTP/KK</span>"

            is_valid = (str(row["Hasil Investigasi"]).strip().upper() == "VALID")

            # Baris tabel merah muda bila INVALID
            if is_valid:
                row_bg = "#ffffff"
                border_b = "1px solid #cbd5e1"
                ket_style = "color: #334155; font-weight: 600;"
            else:
                row_bg = "#fee2e2"
                border_b = "1px solid #fca5a5"
                ket_style = "color: #991b1b; font-weight: 700;"

            badge_inv = '<div style="background-color: #86efac; color: #065f46; font-weight: 800; text-align: center; padding: 6px 10px; border-radius: 6px; font-size: 12px; border: 1px solid #4ade80;">VALID</div>' if is_valid else '<div style="background-color: #fca5a5; color: #991b1b; font-weight: 800; text-align: center; padding: 6px 10px; border-radius: 6px; font-size: 12px; border: 1px solid #f87171;">INVALID</div>'
            ket_txt = row["Penjelasan Invalid"] if str(row["Penjelasan Invalid"]).strip() else "-"

            table_rows.append(
                f"<tr style='border-bottom: {border_b}; background-color: {row_bg}; font-size: 13px;'>"
                f"<td style='padding: 10px 6px; text-align: center; font-weight: bold; border-right: 1px solid #e2e8f0;'>{idx}</td>"
                f"<td style='padding: 10px 8px; white-space: nowrap; border-right: 1px solid #e2e8f0;'>{r_link}</td>"
                f"<td style='padding: 10px 8px; font-weight: 600; border-right: 1px solid #e2e8f0;'>{row['status kiriman']}</td>"
                # Kolom hanya menampilkan alamat tanpa nama penerima
                f"<td style='padding: 10px 8px; border-right: 1px solid #e2e8f0;'><span style='color: #1e293b; font-size: 12px; font-weight: 500;'>{row['alamat penerima']}</span></td>"
                f"<td style='padding: 10px 8px; white-space: nowrap; text-align: center; border-right: 1px solid #e2e8f0;'>{coord_link}</td>"
                f"<td style='padding: 8px; text-align: center; border-right: 1px solid #e2e8f0;'>{img_orang}</td>"
                f"<td style='padding: 8px; text-align: center; border-right: 1px solid #e2e8f0;'>{img_ktp}</td>"
                f"<td style='padding: 10px 8px; text-align: center; min-width: 100px; border-right: 1px solid #e2e8f0;'>{badge_inv}</td>"
                f"<td style='padding: 10px 10px; {ket_style} font-size: 12px; min-width: 160px;'>{ket_txt}</td>"
                f"</tr>"
            )

        st.markdown(
            f"<div style='margin-bottom: 12px; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 6px rgba(0,0,0,0.12); border: 1px solid #94a3b8;'>"
            f"<div style='background-color: #ffffff; color: #000000; text-align: center; padding: 10px 6px; font-size: 16px; font-weight: 900; letter-spacing: 0.5px; border-bottom: 2px solid #111;'>{header_label}</div>"
            f"<table style='width: 100%; border-collapse: collapse; font-family: sans-serif;'>"
            f"<thead><tr style='background-color: {header_bg}; color: #ffffff; font-size: 13px; text-align: center;'>"
            f"<th style='padding: 10px 6px;'>NO</th><th>Nomor Resi</th><th>Status Kiriman</th><th>Alamat</th><th>Koordinat</th><th>Foto Orang</th><th>Foto KTP / KK</th><th>Status</th><th>Keterangan Pengawas</th>"
            f"</tr></thead>"
            f"<tbody>{''.join(table_rows)}</tbody>"
            f"</table>"
            f"</div>",
            unsafe_allow_html=True
        )

    # TERAPKAN FILTER TAMPILAN PADA TABEL
    if filter_status == "⚠️ Hanya INVALID (Perlu Cek)":
        df_tampil = df_ma[df_ma["Hasil Investigasi"] == "INVALID"].copy()
    elif filter_status == "✅ Hanya VALID":
        df_tampil = df_ma[df_ma["Hasil Investigasi"] == "VALID"].copy()
    else:
        df_tampil = df_ma.copy()

    if df_tampil.empty:
        st.info(f"Tidak ada data dengan status **{filter_status}**.")
    else:
        urutan_prioritas = df_summary["Petugas & Kantor"].tolist()
        pengantar_aktif = set(df_tampil["pengantar_label"].dropna().unique())
        pengantar_terfilter = [p for p in urutan_prioritas if p in pengantar_aktif]

        WARNA_LIST = ["#002060", "#c00000"]

        for idx_p, p_label in enumerate(pengantar_terfilter):
            df_sub = df_tampil[df_tampil["pengantar_label"] == p_label]
            if df_sub.empty:
                continue

            warna_hdr = WARNA_LIST[idx_p % 2]
            render_tabel_kartu_ma(p_label, df_sub, header_bg=warna_hdr)

            c_ai, c_edit = st.columns([1, 2])

            # 1. TOMBOL PERIKSA AI GEMINI
            with c_ai:
                if st.button(f"✨ Jalankan AI Gemini", key=f"btn_ai_{idx_p}"):
                    total_resi = len(df_sub)
                    prog_bar = st.progress(0)
                    txt_status = st.empty()

                    for c_idx, (_, r_data) in enumerate(df_sub.iterrows(), start=1):
                        resi_curr = r_data["connote"]
                        txt_status.caption(f"Memeriksa {resi_curr} ({c_idx}/{total_resi})...")
                        f_target = r_data.get("foto_ktp")

                        if not f_target:
                            st_ai, al_ai = "INVALID", "Foto identitas nihil"
                        else:
                            is_v, alasan = analyze_document_with_gemini(f_target)
                            st_ai = "VALID" if is_v else "INVALID"
                            al_ai = alasan

                        st.session_state["manual_overrides"][resi_curr] = {
                            "status": st_ai,
                            "ket": al_ai
                        }
                        st.session_state["audit_data"].loc[
                            st.session_state["audit_data"]["connote"] == resi_curr,
                            ["Hasil Investigasi", "Penjelasan Invalid"]
                        ] = [st_ai, al_ai]

                        prog_bar.progress(c_idx / total_resi)

                    txt_status.empty()
                    prog_bar.empty()
                    st.success(f"Analisis Gemini untuk {p_label} selesai!")
                    st.rerun()

            # 2. KOREKSI STATUS MANUAL
            with c_edit:
                with st.expander(f"✍️ Koreksi Manual ({p_label})", expanded=False):
                    list_resi_p = df_sub["connote"].tolist()
                    resi_pilih = st.selectbox("Pilih Resi:", options=list_resi_p, key=f"sel_r_{idx_p}")

                    data_resi_aktif = df_sub[df_sub["connote"] == resi_pilih].iloc[0]

                    with st.form(key=f"form_koreksi_{idx_p}_{resi_pilih}"):
                        ce1, ce2 = st.columns([1, 2])
                        with ce1:
                            st_edit = st.selectbox(
                                "Status:",
                                ["VALID", "INVALID"],
                                index=0 if data_resi_aktif["Hasil Investigasi"] == "VALID" else 1
                            )
                        with ce2:
                            ket_edit = st.text_input(
                                "Keterangan Pengawas:",
                                value=data_resi_aktif["Penjelasan Invalid"]
                            )

                        btn_simpan = st.form_submit_button("💾 Simpan Perubahan")

                        if btn_simpan:
                            st.session_state["manual_overrides"][resi_pilih] = {
                                "status": st_edit,
                                "ket": ket_edit
                            }
                            st.session_state["audit_data"].loc[
                                st.session_state["audit_data"]["connote"] == resi_pilih,
                                ["Hasil Investigasi", "Penjelasan Invalid"]
                            ] = [st_edit, ket_edit]
                            st.success(f"Resi {resi_pilih} berhasil diperbarui!")
                            st.rerun()

            st.markdown("<div style='margin-bottom: 25px;'></div>", unsafe_allow_html=True)