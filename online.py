import pulp
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import os
import warnings
import traceback
import time
import json
from xml.etree import ElementTree
import holidays
import glob

warnings.simplefilter(action='ignore', category=UserWarning)

# ==============================================================================
# KONFIGURACE SYSTÉMU
# ==============================================================================
TOKEN_SOLAX = os.environ.get("TOKEN_SOLAX")
WIFI_SN = os.environ.get("SN")
LAT, LON = "49.848", "18.409"
DECLINATION, AZIMUTH = "35", "-50"
KW_PEAK = 10.0
KAPACITA_BATERIE_KWH = 10.0
MAX_VYKON_STRIDACE = 10.0

# --- NASTAVENÍ MEZÍ BATERIE ---
SOC_MIN = 15.0  # Minimální nabití (bezpečnostní rezerva pro dům a střídač)
SOC_MAX = 100.0 # Maximální nabití

# --- KONFIGURACE BOJLERU ---
BOJLER_KW = 2.0
BOJLER_HODIN_DENNE = 2.0
BOJLER_CELKEM_INTERVALU = int(BOJLER_HODIN_DENNE * 4) 

SOUBOR_PREDPOVEDI = "predpoved_cache.json"
SOUBOR_PREDPOVEDI_PVF = "predpoved_pvf_cache.json"
SOUBOR_CENY = "ceny_cache.json"
MIN_DNI_PRO_UCENI = 2

# ==============================================================================
# POMOCNÉ FUNKCE
# ==============================================================================

def bezpecny_float(val):
    try:
        if pd.isna(val): return 0.0
        return float(str(val).replace(' ', '').replace(',', '.'))
    except: return 0.0

def nacti_solax_v2():
    url = "https://global.solaxcloud.com/proxyApp/proxy/api/v2/dataAccess/realtimeInfo/get"
    payload = {"wifiSn": WIFI_SN}
    headers = {"tokenId": TOKEN_SOLAX, "Content-Type": "application/json"}
    for _ in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            data = r.json()
            if data.get("success"):
                res = data.get("result")
                return {
                    "cas_mereni": res.get("uploadTime", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    "v_dnes": float(res.get("yieldtoday", 0)),
                    "soc": float(res.get("soc", 0)),
                    "s_celkem": float(res.get("consumeenergy", 0)),
                    "dc1": float(res.get("powerdc1", 0)),
                    "dc2": float(res.get("powerdc2", 0)),
                    "ac_out": float(res.get("acpower", 0)),
                    "bat_p": float(res.get("batPower", 0)),
                    "sit_w": float(res.get("feedinpower", 0)),
                    "e_celkem": float(res.get("feedinenergy", 0))
                }
        except: time.sleep(5)
    return None

def nacti_ceny_entsoe():
    TOKEN_ENTSOE = "680f2687-dd26-443a-81d1-db067ee6b029"
    DOMENA_CZ = "10YCZ-CEPS-----N"
    cas_utc = datetime.now(timezone.utc)
    ceny, data = {}, {}
    if os.path.exists(SOUBOR_CENY):
        try:
            with open(SOUBOR_CENY, 'r') as f: data = json.load(f)
            last_dl = data.get("_last_download")
            if last_dl and datetime.now() - datetime.fromisoformat(last_dl) < timedelta(hours=6):
                return {pd.to_datetime(k).to_pydatetime(): v for k, v in data["ceny"].items()}
        except: pass
    start = (cas_utc - timedelta(days=1)).strftime("%Y%m%d0000")
    stop = (cas_utc + timedelta(days=3)).strftime("%Y%m%d0000")
    url = f"https://web-api.tp.entsoe.eu/api?securityToken={TOKEN_ENTSOE}&documentType=A44&in_Domain={DOMENA_CZ}&out_Domain={DOMENA_CZ}&periodStart={start}&periodEnd={stop}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            root = ElementTree.fromstring(r.content)
            zaznamy = []
            for ts in root.findall('.//{*}TimeSeries'):
                period = ts.find('.//{*}Period')
                reso = period.find('.//{*}resolution').text
                krok = 15 if reso == 'PT15M' else 60
                start_dt = pd.to_datetime(period.find('.//{*}start').text)
                for point in period.findall('.//{*}Point'):
                    pos = int(point.find('{*}position').text)
                    price = float(point.find('{*}price.amount').text)
                    cas_local = (start_dt + timedelta(minutes=(pos - 1) * krok)).tz_convert('Europe/Prague').tz_localize(None)
                    zaznamy.append({"Cas": cas_local, "Cena": price})
            if zaznamy:
                df = pd.DataFrame(zaznamy).drop_duplicates(subset=["Cas"]).set_index("Cas").resample("15min").ffill().reset_index()
                for _, row in df.iterrows(): ceny[row["Cas"].to_pydatetime()] = row["Cena"]
                with open(SOUBOR_CENY, 'w') as f:
                    json.dump({"_last_download": datetime.now().isoformat(), "ceny": {k.strftime('%Y-%m-%d %H:%M:%S'): v for k, v in ceny.items()}}, f)
    except: pass
    return ceny

def nacti_predpoved_fs():
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved = {}
    data, stara_data = None, None
    if os.path.exists(SOUBOR_PREDPOVEDI):
        try:
            with open(SOUBOR_PREDPOVEDI, 'r') as f: 
                stara_data = json.load(f)
                posledni_stazeni = datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")).date()
                if posledni_stazeni == datetime.now().date(): data = stara_data
        except: pass
    if not data:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                novy_json = r.json()
                if 'result' in novy_json and 'watts' in novy_json['result']:
                    novy_json["_last_download"] = datetime.now().isoformat()
                    with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(novy_json, f)
                    data = novy_json
                else: data = stara_data
            else: data = stara_data
        except: data = stara_data
    if not data or 'result' not in data: return predpoved
    try:
        raw_data = []
        for cas_str, w in data['result']['watts'].items():
            raw_data.append({"Cas": pd.to_datetime(cas_str).replace(tzinfo=None), "W": float(w)})
        if raw_data:
            df = pd.DataFrame(raw_data).set_index("Cas").resample("5min").mean().interpolate().fillna(0.0)
            for c, r in df.iterrows(): predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: pass
    return predpoved

def nacti_predpoved_pvf():
    url = f"https://www.pvforecast.cz/api/?key=8slpgw&lat={LAT}&lon={LON}&format=json"
    predpoved = {}
    data, stara_data = None, None
    if os.path.exists(SOUBOR_PREDPOVEDI_PVF):
        try:
            with open(SOUBOR_PREDPOVEDI_PVF, 'r') as f: 
                stara_data = json.load(f)
                posledni_stazeni = datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")).date()
                if posledni_stazeni == datetime.now().date(): data = stara_data
        except: pass
    if not data:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                raw_json = r.json()
                if isinstance(raw_json, list) and len(raw_json) > 10:
                    data = {"_last_download": datetime.now().isoformat(), "forecast": raw_json}
                    with open(SOUBOR_PREDPOVEDI_PVF, 'w') as f: json.dump(data, f)
                else: data = stara_data
            else: data = stara_data
        except: data = stara_data
    if not data or 'forecast' not in data: return predpoved
    try:
        raw = []
        for item in data['forecast']:
            raw.append({"Cas": pd.to_datetime(item[0]).replace(tzinfo=None), "W": bezpecny_float(item[1]) * KW_PEAK})
        if raw:
            df = pd.DataFrame(raw).set_index("Cas").resample("5min").mean().interpolate().fillna(0.0)
            for c, r in df.iterrows(): predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: pass
    return predpoved

def nauc_se_korekci(df_h, sloupec_predpovedi):
    korekce = {h: 1.0 for h in range(24)}
    if df_h.empty or 'Celkovy_Vykon_Panelu_W' not in df_h.columns or sloupec_predpovedi not in df_h.columns: return korekce
    try:
        df_k = df_h[['Cas', 'Celkovy_Vykon_Panelu_W', sloupec_predpovedi]].copy()
        df_k['Real'] = pd.to_numeric(df_k['Celkovy_Vykon_Panelu_W'].astype(str).str.replace(',', '.'), errors='coerce')
        df_k['Pred'] = pd.to_numeric(df_k[sloupec_predpovedi].astype(str).str.replace(',', '.'), errors='coerce')
        df_k['Cas_Parsed'] = pd.to_datetime(df_k['Cas'], format='mixed', errors='coerce') 
        df_k = df_k.dropna(subset=['Cas_Parsed'])
        df_k['Hodina'] = df_k['Cas_Parsed'].dt.hour
        agregace = df_k.groupby('Hodina')[['Real', 'Pred']].sum()
        for hodina, row in agregace.iterrows():
            if row['Pred'] > 50:
                korekce[hodina] = max(0.2, min(6.0, row['Real'] / row['Pred']))
    except: pass
    return korekce

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_W' not in df_h.columns: return None
    try:
        df_h_temp = df_h.copy()
        df_h_temp['Cas_Parsed'] = pd.to_datetime(df_h_temp['Cas'], format='mixed', errors='coerce') 
        df_h_temp = df_h_temp.dropna(subset=['Cas_Parsed'])
        df_temp = df_h_temp[df_h_temp['Cas_Parsed'] >= (aktualni_cas - timedelta(days=90))].copy()
        cz_holidays = holidays.CZ(years=[aktualni_cas.year])
        def urci_typ(dt):
            if dt.date() in cz_holidays: return "Svatek"
            return ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek", "Sobota", "Nedele"][dt.weekday()]
        cilovy_typ = urci_typ(aktualni_cas)
        df_temp['Typ_Dne'] = df_temp['Cas_Parsed'].apply(urci_typ)
        df_f = df_temp[(df_temp['Cas_Parsed'].dt.hour == aktualni_cas.hour) & (df_temp['Cas_Parsed'].dt.minute // 15 == aktualni_cas.minute // 15)]
        df_final = df_f[df_f['Typ_Dne'] == cilovy_typ]
        if df_final['Cas_Parsed'].dt.date.nunique() < MIN_DNI_PRO_UCENI:
            pracovni = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
            df_final = df_f[df_f['Typ_Dne'].isin(pracovni if cilovy_typ in pracovni else ["Sobota", "Nedele", "Svatek"])]
        if df_final['Cas_Parsed'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
            val = pd.to_numeric(df_final['Skutecna_Spotreba_W'].astype(str).str.replace(',', '.'), errors='coerce').mean()
            return max(0.0, val / 1000.0)
    except: pass
    return None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_Z_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0: return "NABIJET_SOLAREM"
    elif soc > 20: return "VYBIJET_PRO_DUM"
    return "NORMALNI_PROVOZ"

def vygeneruj_duvod_pulp(akce, cena, pv, soc):
    if akce == "PRODAVAT_Z_BATERII": 
        return f"Vysoka cena ({cena:.2f} EUR), vyuziti kapacity pro zisk."
    if akce == "POKRYT_Z_BATERIE": 
        return f"Kryti spotreby z baterie, cena je {cena:.2f} EUR."
        
    if akce == "NABIJET_ZE_SITE": 
        if cena <= 10.0:
            return f"Absolutne vyhodna cena ({cena:.2f} EUR), plnim baterii ze site."
        else:
            return f"Priprava na drazsi spicku (aktualne {cena:.2f} EUR), nabijim dopredu."
            
    if akce == "NABIJET_SOLAREM": 
        return f"Prebytek slunce ({pv*1000:.0f} W), ukladam energii."
    if akce == "PRODAVAT_DO_SITE": 
        return f"Baterie je plna (SOC {soc} %), prodavam prebytek."
    if akce == "VYBIJET_PRO_DUM": 
        return "Vyuziti ulozene energie pro kryti spotreby domu."
        
    return "Bezny provoz EMS."

# ==============================================================================
# HLAVNÍ PROGRAM
# ==============================================================================

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, second=0, microsecond=0)
    ted_ctvrt = ted.replace(minute=(ted.minute // 15) * 15)
    
    vsechny_soubory = glob.glob("fve_historie_*.csv")
    df_list = [pd.read_csv(f, sep=';', decimal=',') for f in vsechny_soubory]
    df_h = pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()

    odjeto_intervalu = 0
    if not df_h.empty and 'Bojler_Zapnut' in df_h.columns:
        try:
            df_h['Cas_Parsed'] = pd.to_datetime(df_h['Cas'], format='mixed', errors='coerce') 
            dnesni_data = df_h[df_h['Cas_Parsed'].dt.date == ted.date()].copy()
            if not dnesni_data.empty:
                zapnuto_bloky = dnesni_data['Bojler_Zapnut'].astype(str).str.contains('1').sum()
                odjeto_intervalu = zapnuto_bloky // 3
        except: pass

    zbyva_intervalu_dnes = max(0, BOJLER_CELKEM_INTERVALU - odjeto_intervalu)

    korekce_fs = nauc_se_korekci(df_h, 'Predpoved_FS_W')
    korekce_pvf = nauc_se_korekci(df_h, 'Predpoved_PVF_W')
    vsechny_ceny = nacti_ceny_entsoe()
    vsechny_fs = nacti_predpoved_fs()
    vsechny_pvf = nacti_predpoved_pvf()

    ceny_192, pv_192, spotreba_192, casy_192 = [], [], [], []
    for i in range(192):
        c = ted_ctvrt + timedelta(minutes=15 * i)
        casy_192.append(c)
        pv_192.append(min(vsechny_fs.get(c, 0.0) * korekce_fs.get(c.hour, 1.0), KW_PEAK))
        ceny_192.append(vsechny_ceny.get(c, 0.0))
        spotreba_192.append(nauc_se_spotrebu(df_h, c) or 0.0)

    # --- PŘIDÁNO MASKOVÁNÍ SOC ---
    skutecne_soc = bezpecny_float(df_h.iloc[-1].get('Baterie_SOC_%', 50.0)) if not df_h.empty else 50.0
    p_soc = max(skutecne_soc, SOC_MIN)
    
    model = pulp.LpProblem("EMS", pulp.LpMinimize)
    p_nab = pulp.LpVariable.dicts("Nab", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vyb = pulp.LpVariable.dicts("Vyb", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_nakup = pulp.LpVariable.dicts("Nakup", range(192), lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", range(192), lowBound=0)
    
    soc_vars = pulp.LpVariable.dicts("SOC", range(192), lowBound=SOC_MIN, upBound=SOC_MAX)
    
    is_chg = pulp.LpVariable.dicts("IsChg", range(192), cat=pulp.LpBinary)
    b_on = pulp.LpVariable.dicts("Bojler", range(192), cat=pulp.LpBinary)

    for i in range(192):
        model += (pv_192[i] + p_nakup[i] + p_vyb[i] == spotreba_192[i] + BOJLER_KW * b_on[i] + p_prodej[i] + p_nab[i])
        zmena = ((p_nab[i] - p_vyb[i]) * 0.25 / KAPACITA_BATERIE_KWH) * 100.0
        model += soc_vars[i] == (p_soc if i==0 else soc_vars[i-1]) + zmena
        model += p_nab[i] <= MAX_VYKON_STRIDACE * is_chg[i]
        model += p_vyb[i] <= MAX_VYKON_STRIDACE * (1 - is_chg[i])

    indexy_dneska = [i for i, c in enumerate(casy_192) if c.date() == ted.date()]
    model += pulp.lpSum([b_on[i] for i in indexy_dneska]) == min(zbyva_intervalu_dnes, len(indexy_dneska))
    
    indexy_zitra = [i for i, c in enumerate(casy_192) if c.date() == (ted + timedelta(days=1)).date()]
    model += pulp.lpSum([b_on[i] for i in indexy_zitra]) == BOJLER_CELKEM_INTERVALU

    model += pulp.lpSum([(p_nakup[i]*(ceny_192[i]+60) - p_prodej[i]*(ceny_192[i]-10))*0.25 for i in range(192)])
    model.solve(pulp.PULP_CBC_CMD(msg=False))

    m = nacti_solax_v2()
    if not m: return

    h_spotreba_w = int(round(max(0, m['ac_out'] - m['sit_w'])))
    denni_import_kwh = 0.0
    denni_export_kwh = 0.0
    if not df_h.empty:
        df_h['Cas_Parsed_M'] = pd.to_datetime(df_h['Cas'], format='mixed', errors='coerce') 
        dnesni_data_m = df_h[df_h['Cas_Parsed_M'].dt.date == ted.date()]
        if not dnesni_data_m.empty:
            denni_import_kwh = max(0.0, m['s_celkem'] - bezpecny_float(dnesni_data_m.iloc[0].get('Spotreba_Celkem_kWh', m['s_celkem'])))
            denni_export_kwh = max(0.0, m['e_celkem'] - bezpecny_float(dnesni_data_m.iloc[0].get('Export_Celkem_kWh', m['e_celkem'])))

    akce_heuristika = rozhodovaci_logika(pv_192[0], spotreba_192[0], m['soc'], ceny_192[0])
    
    nabijeni_w = p_nab[0].varValue * 1000
    vybijeni_w = p_vyb[0].varValue * 1000
    nakup_w = p_nakup[0].varValue * 1000
    prodej_w = p_prodej[0].varValue * 1000

    if nabijeni_w > 100 and nakup_w > 100: akce_pulp = "NABIJET_ZE_SITE"
    elif vybijeni_w > 100 and prodej_w > 100: akce_pulp = "PRODAVAT_Z_BATERII"
    elif prodej_w > 100: akce_pulp = "PRODAVAT_DO_SITE"
    elif nabijeni_w > 100: akce_pulp = "NABIJET_SOLAREM"
    elif vybijeni_w > 100: akce_pulp = "VYBIJET_PRO_DUM"
    else: akce_pulp = "NORMALNI_PROVOZ"
    
    duvod = vygeneruj_duvod_pulp(akce_pulp, ceny_192[0], pv_192[0], m['soc'])
    bojler_aktualni_stav = int(round(b_on[0].varValue))
    if bojler_aktualni_stav: duvod += " | Bojler: ZAPNUT"

    ted_5min = ted.replace(minute=(ted.minute // 5) * 5)
    n_radek = pd.DataFrame([{
        'Cas': m['cas_mereni'], 
        'Skutecna_Spotreba_W': h_spotreba_w,
        'Odhad_Spotreba_Modelu_W': int(round(spotreba_192[0] * 1000)),
        'Aktualni_import/export_W': str(m['sit_w']).replace('.', ','),
        'Aktualni_AC_Vystup_W': str(m['ac_out']).replace('.', ','),
        'Celkovy_Vykon_Panelu_W': int(m['dc1']+m['dc2']),
        'Predpoved_FS_W': int(round(vsechny_fs.get(ted_5min, 0.0) * 1000)),
        'Predpoved_PVF_W': int(round(vsechny_pvf.get(ted_5min, 0.0) * 1000)),
        'Vykon_Baterie_W': int(m['bat_p']),
        'Baterie_SOC_%': str(m['soc']).replace('.', ','),
        'Simulovane_SOC_%': str(round(float(soc_vars[0].varValue), 1)).replace('.', ','),
        'Cena_EUR/MWh': str(round(ceny_192[0], 2)).replace('.', ','),
        'Doporucena_Akce': akce_heuristika, 
        'Akce_PuLP': akce_pulp,
        'Duvod_PuLP': duvod,
        'Skutecny_AC_Vystup_kWh': str(round(m['v_dnes'], 4)).replace('.', ','),
        'Cista_Vyroba_Panelu_kWh': str(round((m['dc1']+m['dc2'])/1000*0.0833, 4)).replace('.', ','),
        'Import_5min_kWh': str(round((abs(m['sit_w'])/1000*0.0833 if m['sit_w']<0 else 0), 4)).replace('.', ','),
        'Export_5min_kWh': str(round((m['sit_w']/1000*0.0833 if m['sit_w']>0 else 0), 4)).replace('.', ','),
        'Denni_Import_kWh': str(round(denni_import_kwh, 2)).replace('.', ','),
        'Denni_Export_kWh': str(round(denni_export_kwh, 2)).replace('.', ','),
        'AC_vyroba_Dnes_kWh': str(m['v_dnes']).replace('.', ','),
        'Spotreba_Celkem_kWh': str(m['s_celkem']).replace('.', ','),
        'Export_Celkem_kWh': str(m['e_celkem']).replace('.', ','),
        'Uceni_Koeficient_FS': str(round(korekce_fs.get(ted.hour, 1.0), 2)).replace('.', ','),
        'Uceni_Koeficient_PVF': str(round(korekce_pvf.get(ted.hour, 1.0), 2)).replace('.', ','),
        'Korigovana_Predpoved_FS_W': int(round(min(vsechny_fs.get(ted_5min, 0.0) * 1000 * korekce_fs.get(ted.hour, 1.0), KW_PEAK * 1000))),
        'Korigovana_Predpoved_PVF_W': int(round(min(vsechny_pvf.get(ted_5min, 0.0) * 1000 * korekce_pvf.get(ted.hour, 1.0), KW_PEAK * 1000))),
        'Bojler_Zapnut': bojler_aktualni_stav
    }])

    poradi = [
        'Cas', 'Skutecna_Spotreba_W', 'Odhad_Spotreba_Modelu_W', 'Aktualni_import/export_W', 
        'Aktualni_AC_Vystup_W', 'Celkovy_Vykon_Panelu_W', 'Predpoved_FS_W', 'Predpoved_PVF_W', 
        'Vykon_Baterie_W', 'Baterie_SOC_%', 'Simulovane_SOC_%', 'Cena_EUR/MWh', 'Doporucena_Akce', 
        'Akce_PuLP', 'Duvod_PuLP', 'Skutecny_AC_Vystup_kWh', 'Cista_Vyroba_Panelu_kWh', 
        'Import_5min_kWh', 'Export_5min_kWh', 'Denni_Import_kWh', 'Denni_Export_kWh', 
        'AC_vyroba_Dnes_kWh', 'Spotreba_Celkem_kWh', 'Export_Celkem_kWh',
        'Uceni_Koeficient_FS', 'Uceni_Koeficient_PVF',
        'Korigovana_Predpoved_FS_W', 'Korigovana_Predpoved_PVF_W', 'Bojler_Zapnut'
    ]
    
    n_radek = n_radek[poradi]
    aktualni_soubor = f"fve_historie_{ted.strftime('%Y_%m')}.csv"
    n_radek.to_csv(aktualni_soubor, mode='a', header=not os.path.exists(aktualni_soubor), index=False, sep=';', decimal=',')

if __name__ == "__main__":
    try: main()
    except: traceback.print_exc()
