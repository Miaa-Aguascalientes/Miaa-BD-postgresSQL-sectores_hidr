import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from sqlalchemy import create_engine, inspect, text

# ==============================================================================
# CONFIGURACIÓN DE PÁGINA Y ESTILO SCADA HUD
# ==============================================================================
st.set_page_config(
    page_title="Sincronizador Indicadores -> PostGIS | MIAA",
    page_icon="💧",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp {
        background-color: #050a10;
        color: #e0f2fe;
    }
    [data-testid="stSidebar"] {
        background-color: #081421;
        border-right: 1px solid #00d4ff33;
    }
    h1, h2, h3 {
        color: #00d4ff !important;
        font-family: 'Segoe UI', sans-serif;
    }
    .hud-card {
        background-color: #0b1a29;
        border: 1px solid #00d4ff55;
        border-left: 4px solid #00d4ff;
        border-radius: 6px;
        padding: 14px 18px;
        margin-bottom: 12px;
    }
    .hud-title {
        font-size: 0.80rem;
        color: #7dd3fc;
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }
    .hud-value {
        font-size: 1.35rem;
        font-weight: 700;
        color: #00FF00;
    }
    .stButton > button {
        background-color: #00d4ff;
        color: #050a10;
        font-weight: 700;
        border: none;
        border-radius: 5px;
        padding: 0.55rem 1.2rem;
        transition: all 0.2s ease-in-out;
    }
    .stButton > button:hover {
        background-color: #00FF00;
        color: #000000;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ==============================================================================
# CONSTANTES DE ORIGEN Y DESTINO
# ==============================================================================
SHEET_ID = "1Ovj2DZ19Y8H4BtamnVwB71c2dyHC3dyVmau-tMbWre8"
GID = "1820360811"
SHEET_NAME = "Indicadores"
CSV_EXPORT_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={GID}"

TARGET_SCHEMA = "Sectorizacion"
TARGET_TABLE = "sectores_hidr"

# Columnas esperadas (excluyendo geom para proteger la geometría espacial en PostGIS)
COLUMNAS_ATRIBUTOS = [
    "fid",
    "sector",
    "Pozos_Sector",
    "Cantidad_Pozos",
    "AREA",
    "USUARIOS",
    "USU_DOM",
    "POB_2025",
    "Q_PROD",
    "VOL_PROD-MES",
    "LONG_RED",
    "CONS_PROM",
    "VOL_FACT",
    "DOM_A",
    "DOM_B",
    "DOM_C",
    "COMERCIAL",
    "FALTAS_AGUA",
    "FUGAS",
    "DOTACION",
    "BAL_VOL",
    "EF",
    "ANF",
    "IMP-FACT_US",
    "PERD_US_MES",
    "PERD_HAB_DIA",
    "NIV_MOROSIDAD",
    "FA_USUARIO",
    "FUGAS_KM",
]

# Columnas de texto (el resto se limpian como numéricas si vienen formateadas en Sheets)
COLUMNAS_TEXTO = {"sector", "Pozos_Sector"}


# ==============================================================================
# CONEXIÓN A BASE DE DATOS POSTGRESQL / POSTGIS
# ==============================================================================
@st.cache_resource
def get_pg_engine():
    cfg = st.secrets["db_postgres"]
    db_url = (
        f"postgresql+psycopg2://{cfg['user']}:{cfg['pass']}"
        f"@{cfg['host']}:{cfg['port']}/{cfg['db']}"
    )
    return create_engine(db_url, pool_pre_ping=True)


# ==============================================================================
# EXTRACCIÓN Y LIMPIEZA DE GOOGLE SHEETS
# ==============================================================================
@st.cache_data(ttl=60)
def cargar_datos_sheets(url: str) -> pd.DataFrame:
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    response.encoding = "utf-8"
    df = pd.read_csv(io.StringIO(response.text))
    # Limpiar espacios accidentales en encabezados
    df.columns = [str(c).strip() for c in df.columns]
    return df


def limpiar_valor_numerico(val):
    if pd.isna(val):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if s in ("", "-", "N/A", "n/a", "#DIV/0!", "#VALUE!", "#N/A", "None", "nan"):
        return None
    # Quitar símbolos de moneda, porcentaje, espacios y separadores de miles
    s_clean = re.sub(r"[$,%\s]", "", s)
    # Si tiene comas como separador de miles (ej. 1,234.56)
    if "," in s_clean and "." in s_clean:
        s_clean = s_clean.replace(",", "")
    elif "," in s_clean and "." not in s_clean:
        # Si solo hay una coma y tiene 3 dígitos después, es separador de miles
        parts = s_clean.split(",")
        if len(parts) == 2 and len(parts[1]) == 3:
            s_clean = s_clean.replace(",", "")
        else:
            s_clean = s_clean.replace(",", ".")
    try:
        return float(s_clean)
    except ValueError:
        return None


def preparar_dataframe(df_raw: pd.DataFrame) -> pd.DataFrame:
    df = df_raw.copy()

    # Ignorar geom si viene en la hoja para no tocar la geometría en PostgreSQL
    if "geom" in df.columns:
        df = df.drop(columns=["geom"])

    # Filtrar filas completamente vacías en llaves principales
    if "fid" in df.columns and "sector" in df.columns:
        df = df.dropna(subset=["fid", "sector"], how="all")
    elif "fid" in df.columns:
        df = df.dropna(subset=["fid"])

    for col in df.columns:
        if col == "fid":
            df[col] = df[col].apply(
                lambda x: int(float(limpiar_valor_numerico(x)))
                if limpiar_valor_numerico(x) is not None
                else None
            )
        elif col in COLUMNAS_TEXTO:
            df[col] = df[col].apply(
                lambda x: None if pd.isna(x) or str(x).strip() == "" else str(x).strip()
            )
        else:
            df[col] = df[col].apply(limpiar_valor_numerico)

    return df


# ==============================================================================
# INSPECCIÓN Y SINCRONIZACIÓN CON POSTGRESQL
# ==============================================================================
def obtener_columnas_tabla_pg(engine) -> dict:
    inspector = inspect(engine)
    cols = inspector.get_columns(TARGET_TABLE, schema=TARGET_SCHEMA)
    return {c["name"]: str(c["type"]) for c in cols}


def sincronizar_postgres(engine, df: pd.DataFrame, clave_cruce: str):
    cols_pg = obtener_columnas_tabla_pg(engine)
    if not cols_pg:
        raise RuntimeError(
            f'No se encontró la tabla "{TARGET_SCHEMA}"."{TARGET_TABLE}" o no tiene columnas visibles.'
        )

    # Mapeo insensible a mayúsculas/minúsculas para respetar nombres exactos en PostgreSQL
    mapa_pg_lower = {k.lower(): k for k in cols_pg.keys()}

    columnas_validas = []
    for col_sheet in df.columns:
        if col_sheet.lower() == "geom":
            continue
        if col_sheet in cols_pg:
            columnas_validas.append((col_sheet, col_sheet))
        elif col_sheet.lower() in mapa_pg_lower:
            columnas_validas.append((col_sheet, mapa_pg_lower[col_sheet.lower()]))

    col_clave_pg = cols_pg.get(clave_cruce) and clave_cruce or mapa_pg_lower.get(clave_cruce.lower())
    if not col_clave_pg:
        raise ValueError(f"La llave de cruce '{clave_cruce}' no existe en la tabla de PostgreSQL.")

    cols_update = [
        (c_sheet, c_pg)
        for c_sheet, c_pg in columnas_validas
        if c_pg != col_clave_pg
    ]

    if not cols_update:
        raise ValueError("No hay columnas coincidentes para actualizar entre Google Sheets y PostgreSQL.")

    # Construir sentencia SQL parametrizada con identificadores entrecomillados
    set_clauses = []
    for idx, (_, c_pg) in enumerate(cols_update):
        param_name = f"p_{idx}"
        # Si en Postgres la columna es entera, redondear/castear limpiamente
        tipo_pg = cols_pg[c_pg].upper()
        if any(t in tipo_pg for t in ("INT", "BIGINT", "SMALLINT")):
            set_clauses.append(f'"{c_pg}" = CAST(:{param_name} AS BIGINT)')
        else:
            set_clauses.append(f'"{c_pg}" = :{param_name}')

    sql_update = text(
        f"""
        UPDATE "{TARGET_SCHEMA}"."{TARGET_TABLE}"
        SET {", ".join(set_clauses)}
        WHERE "{col_clave_pg}" = :key_val
        """
    )

    actualizados = 0
    sin_coincidencia = []
    omitidos_sin_llave = 0

    with engine.begin() as conn:
        for row_idx, row in df.iterrows():
            key_val = row.get(clave_cruce)
            if key_val is None or (isinstance(key_val, float) and pd.isna(key_val)):
                omitidos_sin_llave += 1
                continue

            if clave_cruce == "fid":
                key_val = int(key_val)
            else:
                key_val = str(key_val).strip()

            params = {"key_val": key_val}
            for idx, (c_sheet, c_pg) in enumerate(cols_update):
                val = row[c_sheet]
                if pd.isna(val):
                    val = None
                elif any(t in cols_pg[c_pg].upper() for t in ("INT", "BIGINT", "SMALLINT")) and val is not None:
                    val = int(round(float(val)))
                params[f"p_{idx}"] = val

            res = conn.execute(sql_update, params)
            if res.rowcount > 0:
                actualizados += res.rowcount
            else:
                sin_coincidencia.append(f"Fila {row_idx + 2} ({clave_cruce}={key_val})")

    return {
        "actualizados": actualizados,
        "sin_coincidencia": sin_coincidencia,
        "omitidos_sin_llave": omitidos_sin_llave,
        "columnas_mapeadas": [c_pg for _, c_pg in cols_update],
    }


# ==============================================================================
# INTERFAZ DE USUARIO
# ==============================================================================
st.title("💧 Sincronización de Indicadores | Google Sheets → PostGIS")
st.caption(
    f'Origen: Hoja "{SHEET_NAME}" (GID {GID})  |  Destino: "{TARGET_SCHEMA}"."{TARGET_TABLE}" (ti.miaa.mx)'
)

with st.sidebar:
    st.header("⚙️ Configuración")
    clave_cruce = st.selectbox(
        "Llave primaria de cruce (WHERE)",
        options=["fid", "sector"],
        index=0,
        help="Columna usada para localizar cada registro en PostgreSQL sin alterar la geometría (geom).",
    )
    st.info(
        "🛡️ **Protección Geoespacial Activa:** La columna `geom` se omite automáticamente durante la transferencia para preservar los polígonos en QGIS."
    )
    if st.button("🔄 Recargar Google Sheets", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

# Cargar datos de Google Sheets
try:
    df_raw = cargar_datos_sheets(CSV_EXPORT_URL)
    df_clean = preparar_dataframe(df_raw)
except Exception as e:
    st.error(f"❌ Error al leer la hoja '{SHEET_NAME}' desde Google Sheets: {e}")
    st.stop()

# Métricas superiores
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.markdown(
        f'<div class="hud-card"><div class="hud-title">Registros en Sheets</div><div class="hud-value">{len(df_clean)}</div></div>',
        unsafe_allow_html=True,
    )
with col2:
    st.markdown(
        f'<div class="hud-card"><div class="hud-title">Columnas Detectadas</div><div class="hud-value">{len(df_clean.columns)}</div></div>',
        unsafe_allow_html=True,
    )
with col3:
    st.markdown(
        f'<div class="hud-card"><div class="hud-title">Tabla Destino</div><div class="hud-value" style="font-size:1.05rem;">{TARGET_SCHEMA}.{TARGET_TABLE}</div></div>',
        unsafe_allow_html=True,
    )
with col4:
    st.markdown(
        f'<div class="hud-card"><div class="hud-title">Llave de Cruce</div><div class="hud-value">{clave_cruce}</div></div>',
        unsafe_allow_html=True,
    )

# Diagnóstico de columnas y conexión
tab_preview, tab_schema = st.tabs(["📋 Vista Previa de Datos (Sheets)", "🗄️ Auditoría de Esquema PostgreSQL"])

with tab_preview:
    st.dataframe(df_clean, use_container_width=True, height=380)

with tab_schema:
    try:
        engine = get_pg_engine()
        cols_pg = obtener_columnas_tabla_pg(engine)
        df_audit = pd.DataFrame(
            [
                {
                    "Columna PostgreSQL": col,
                    "Tipo PostgreSQL": dtype,
                    "Presente en Sheets": "✅ Sí"
                    if col.lower() in {c.lower() for c in df_clean.columns}
                    else ("🛡️ Protegida (geom)" if col.lower() == "geom" else "⚠️ No"),
                }
                for col, dtype in cols_pg.items()
            ]
        )
        st.dataframe(df_audit, use_container_width=True, height=380)
    except Exception as e:
        st.error(f"❌ No se pudo conectar a PostgreSQL (`ti.miaa.mx`): {e}")

st.divider()

# Acción principal de transferencia
col_btn, col_info = st.columns([1, 2])
with col_btn:
    ejecutar = st.button("🚀 Transferir Datos a PostgreSQL", use_container_width=True)

if ejecutar:
    hora_mx = datetime.now(ZoneInfo("America/Mexico_City")).strftime("%d/%m/%Y %H:%M:%S")
    with st.spinner("Sincronizando registros con ti.miaa.mx..."):
        try:
            engine = get_pg_engine()
            resultado = sincronizar_postgres(engine, df_clean, clave_cruce=clave_cruce)

            st.success(
                f"✅ Sincronización completada ({hora_mx}) — **{resultado['actualizados']}** registros actualizados en `\"{TARGET_SCHEMA}\".\"{TARGET_TABLE}\"`."
            )

            c_res1, c_res2, c_res3 = st.columns(3)
            c_res1.metric("Filas Actualizadas", resultado["actualizados"])
            c_res2.metric("Columnas Actualizadas", len(resultado["columnas_mapeadas"]))
            c_res3.metric("Sin Coincidencia en BD", len(resultado["sin_coincidencia"]))

            if resultado["sin_coincidencia"]:
                with st.expander(f"⚠️ Ver registros sin coincidencia por `{clave_cruce}` ({len(resultado['sin_coincidencia'])})"):
                    st.write(resultado["sin_coincidencia"])

            if resultado["omitidos_sin_llave"] > 0:
                st.warning(f"Se omitieron {resultado['omitidos_sin_llave']} filas vacías sin `{clave_cruce}`.")

        except Exception as e:
            st.error(f"❌ Error durante la actualización en PostgreSQL: {e}")
