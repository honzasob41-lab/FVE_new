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

# Konfigurace prostředí
TOKEN_SOLAX = os.environ.get("TOKEN_SOLAX")
WIFI_SN = os.environ.get("SN")
LAT, LON = "49.848", "18.409"
DECLINATION, AZIMUTH = "35", "-50"
KW_PEAK = 10.0
KAPACITA_BATERIE_KWH = 10.0
MAX_VYKON_STRIDACE = 10.0

# --- KONFIGURACE BOJLERU ---
BOJLER_KW = 2.0
BOJLER_HODIN_DENNE = 2.0
BOJLER_CELKEM_INTERVALU = int(BOJLER_HODIN_DENNE * 4) # 8 intervalů po 15 min

SOUBOR_PREDPOVEDI = "predpoved_cache.json"
SOUBOR_PREDPOVEDI_PVF = "predpoved_pvf_cache.json"
SOUBOR_CENY = "ceny_cache.json"
MIN_DNI_PRO_UCENI = 2

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
                if datetime.now() - datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")) <= timedelta(hours=3):
                    data = stara_data
        except: pass
        
    if not data:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                novy_json = r.json()
                if 'result' in novy_json and 'watts' in novy_json['result'] and len(novy_json['result']['watts']) > 10:
                    novy_json["_last_download"] = datetime.now().isoformat()
                    with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(novy_json, f)
                    data = novy_json
                else:
                    print("FS API poslalo podezrele malo dat. Pouzivam starou cache.", flush=True)
                    if stara_data: data = stara_data
            else:
                if stara_data: data = stara_data
        except Exception:
            if stara_data: data = stara_data
            
    if not data or 'result' not in data: return predpoved
    try:
        raw_data = []
        for cas_str, w in data['result']['watts'].items():
            cas = pd.to_datetime(cas_str).replace(tzinfo=None)
            raw_data.append({"Cas": cas, "W": float(w)})
        if raw_data:
            df = pd.DataFrame(raw_data).set_index("Cas")
            for den in df.index.normalize().unique():
                ranni_cas = den + pd.Timedelta(hours=4)
                vecerni_cas = den + pd.Timedelta(hours=21)
                if ranni_cas not in df.index: df.loc[ranni_cas] = 0.0
                if vecerni_cas not in df.index: df.loc[vecerni_cas] = 0.0
            df = df.sort_index().resample("5min").mean().interpolate(method='linear').fillna(0.0)
            for c, r in df.iterrows(): predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: traceback.print_exc()
    return predpoved

def nacti_predpoved_pvf():
    url = f"https://www.pvforecast.cz/api/?key=8slpgw&lat={LAT}&lon={LON}&format=json"
    predpoved = {}
    data, stara_data = None, None
    
    if os.path.exists(SOUBOR_PREDPOVEDI_PVF):
        try:
            with open(SOUBOR_PREDPOVEDI_PVF, 'r') as f: 
                stara_data = json.load(f)
                if datetime.now() - datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")) <= timedelta(hours=3):
                    data = stara_data
        except: pass
        
    if not data:
        print("--- START STAHOVANI PV FORECAST ---", flush=True)
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                try: raw_json = r.json()
                except: raw_json = json.loads(r.text)
                
                if isinstance(raw_json, list) and len(raw_json) > 10:
                    print(f"OK: Stazeno {len(raw_json)} platnych zaznamu.", flush=True)
                    data = {"_last_download": datetime.now().isoformat(), "forecast": raw_json}
                    with open(SOUBOR_PREDPOVEDI_PVF, 'w') as f: json.dump(data, f)
                else:
                    print(f"CHYBA DAT: Server poslal jen {len(raw_json) if isinstance(raw_json, list) else 'nesmysl'}. Zneni: {raw_json}", flush=True)
                    if stara_data: data = stara_data
            else:
                print(f"CHYBA SERVERU: Odpoved neni 200 OK, ale kod {r.status_code}. Zprava serveru: {r.text}", flush=True)
                if stara_data: data = stara_data
        except Exception as e:
            print(f"KRITICKA CHYBA SPOJENI: {e}", flush=True)
            if stara_data: data = stara_data
            
    if not data or 'forecast' not in data: return predpoved
    try:
        raw = []
        for item in data['forecast']:
            cas_str = item[0]
            osvit_w_m2 = float(item[1])
            cas = pd.to_datetime(cas_str).replace(tzinfo=None)
            vykon_w = osvit_w_m2 * KW_PEAK
            raw.append({"Cas": cas, "W": vykon_w})
        if raw:
            df = pd.DataFrame(raw).set_index("Cas")
            for den in df.index.normalize().unique():
                ranni_cas = den + pd.Timedelta(hours=4)
                vecerni_cas = den + pd.Timedelta(hours=21)
                if ranni_cas not in df.index: df.loc[ranni_cas] = 0.0
                if vecerni_cas not in df.index: df.loc[vecerni_cas] = 0.0
            df = df.sort_index().resample("5min").mean().interpolate(method='linear').fillna(0.0)
            for c, r in df.iterrows(): predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: traceback.print_exc()
    return predpoved

def nauc_se_korekci(df_h, sloupec_predpovedi):
    korekce = {h: 1.0 for h in range(24)}
    if df_h.empty or 'Celkovy_Vykon_Panelu_W' not in df_h.columns or sloupec_predpovedi not in df_h.columns: return korekce
    try:
        df_k = df_h[['Cas', 'Celkovy_Vykon_Panelu_W', sloupec_predpovedi]].copy()
        df_k['Real'] = pd.to_numeric(df_k['Celkovy_Vykon_Panelu_W'].astype(str).str.replace(',', '.'), errors='coerce')
        df_k['Pred'] = pd.to_numeric(df_k[sloupec_predpovedi].astype(str).str.replace(',', '.'), errors='coerce')
        df_k['Hodina'] = df_k['Cas'].dt.hour
        agregace = df_k.groupby('Hodina')[['Real', 'Pred']].sum()
        for hodina, row in agregace.iterrows():
            if row['Pred'] > 50:
                koef = row['Real'] / row['Pred']
                korekce[hodina] = max(0.2, min(6.0, koef))
    except: pass
    return korekce

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_W' not in df_h.columns: return None
    maska = (df_h['Cas'] >= (aktualni_cas - timedelta(days=90)))
    df_temp = df_h[maska].copy()
    cz_holidays = holidays.CZ(years=[aktualni_cas.year])
    def urci_typ(dt):
        if dt.date() in cz_holidays: return "Svatek"
        return ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek", "Sobota", "Nedele"][dt.weekday()]
    cilovy_typ = urci_typ(aktualni_cas)
    df_temp['Typ_Dne'] = df_temp['Cas'].apply(urci_typ)
    df_f = df_temp[(df_temp['Cas'].dt.hour == aktualni_cas.hour) & (df_temp['Cas'].dt.minute // 15 == aktualni_cas.minute // 15)]
    df_final = df_f[df_f['Typ_Dne'] == cilovy_typ]
    if df_final['Cas'].dt.date.nunique() < MIN_DNI_PRO_UCENI:
        pracovni = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
        df_final = df_f[df_f['Typ_Dne'].isin(pracovni if cilovy_typ in pracovni else ["Sobota", "Nedele", "Svatek"])]
    if df_final['Cas'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
        val = pd.to_numeric(df_final['Skutecna_Spotreba_W'].astype(str).str.replace(',', '.'), errors='coerce').mean()
        return max(0.0, val / 1000.0)
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
    if akce == "PRODAVAT_Z_BATERII": return f"Vysoka cena ({cena:.2f} EUR), vyuziti kapacity pro zisk."
    if akce == "POKRYT_Z_BATERIE": return f"Kryti spotreby z baterie, cena je {cena:.2f} EUR."
    return "Bezny provoz EMS."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, second=0, microsecond=0)
    ted_ctvrt = ted.replace(minute=(ted.minute // 15) * 15)
    
    vsechny_soubory = glob.glob("fve_historie_*.csv")
    df_list = [pd.read_csv(f, sep=';', decimal=',') for f in vsechny_soubory]
    df_h = pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()
    if not df_h.empty:
        df_h['Cas'] = pd.to_datetime(df_h['Cas'], format='mixed', dayfirst=True, errors='coerce')
        df_h = df_h.sort_values(by='Cas').reset_index(drop=True)

    # --- PAMET BOJLERU: Kolik 15min intervalu uz dnes odjel? ---
    odjeto_intervalu = 0
    if not df_h.empty and 'Bojler_Zapnut' in df_h.columns:
        dnesni_data = df_h[df_h['Cas'].dt.date == ted.date()]
        bojler_bezel_5min_bloku = len(dnesni_data[dnesni_data['Bojler_Zapnut'].astype(str).replace('.0', '') == '1'])
        odjeto_intervalu = bojler_bezel_5min_bloku // 3
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
        hodina_c = c.hour
        korigovane_fs = min(vsechny_fs.get(c, 0.0) * korekce_fs.get(hodina_c, 1.0), KW_PEAK)
        pv_192.append(korigovane_fs)
        ceny_192.append(vsechny_ceny.get(c, 0.0))
        spot = nauc_se_spotrebu(df_h, c)
        spotreba_192.append(spot if spot is not None else 0.0)

    p_soc = bezpecny_float(df_h.iloc[-1].get('Baterie_SOC_%', 50.0)) if not df_h.empty else 50.0
    
    model = pulp.LpProblem("EMS", pulp.LpMinimize)
    p_nab = pulp.LpVariable.dicts("Nab", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vyb = pulp.LpVariable.dicts("Vyb", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_nakup = pulp.LpVariable.dicts("Nakup", range(192), lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", range(192), lowBound=0)
    soc_vars = pulp.LpVariable.dicts("SOC", range(192), lowBound=10.0, upBound=100.0)
    is_chg = pulp.LpVariable.dicts("IsChg", range(192), cat=pulp.LpBinary)
    b_on = pulp.LpVariable.dicts("Bojler", range(192), cat=pulp.LpBinary)

    for i in range(192):
        model += (pv_192[i] + p_nakup[i] + p_vyb[i] == spotreba_192[i] + BOJLER_KW * b_on[i] + p_prodej[i] + p_nab[i])
        zmena = ((p_nab[i] - p_vyb[i]) * 0.25 / KAPACITA_BATERIE_KWH) * 100.0
        model += soc_vars[i] == (p_soc if i==0 else soc_vars[i-1]) + zmena
        model += p_nab[i] <= MAX_VYKON_STRIDACE * is_chg[i]
        model += p_vyb[i] <= MAX_VYKON_STRIDACE * (1 - is_chg[i])

    indexy_dneska = [i for i, c in enumerate(casy_192) if c.date() == ted.date()]
    realne_zbyva_dnes = min(zbyva_intervalu_dnes, len(indexy_dneska))
    model += pulp.lpSum([b_on[i] for i in indexy_dneska]) == realne_zbyva_dnes

    indexy_zitra = [i for i, c in enumerate(casy_192) if c.date() == (ted + timedelta(days=1)).date()]
    model += pulp.lpSum([b_on[i] for i in indexy_zitra]) == BOJLER_CELKEM_INTERVALU

    model += pulp.lpSum([(p_nakup[i]*(ceny_192[i]+60) - p_prodej[i]*(ceny_192[i]-10))*0.25 for i in range(192)])
    model.solve(pulp.PULP_CBC_CMD(msg=False))

    m = nacti_solax_v2()
    if not m: return

    denni_import_kwh = 0.0
    denni_export_kwh = 0.0
    h_spotreba_w = 0

    if not df_h.empty:
        dnesni_data = df_h[df_h['Cas'].dt.date == ted.date()]
        if not dnesni_data.empty:
            start_import = bezpecny_float(dnesni_data.iloc[0].get('Spotreba_Celkem_kWh', m['s_celkem']))
            start_export = bezpecny_float(dnesni_data.iloc[0].get('Export_Celkem_kWh', m['e_celkem']))
            denni_import_kwh = max(0.0, m['s_celkem'] - start_import)
            denni_export_kwh = max(0.0, m['e_celkem'] - start_export)

        posledni_s_celkem = bezpecny_float(df_h.iloc[-1].get('Spotreba_Celkem_kWh', m['s_celkem']))
        rozdil_kwh = m['s_celkem'] - posledni_s_celkem
        if 0 < rozdil_kwh < 10: h_spotreba_w = int(round(rozdil_kwh * 12000))
        else: h_spotreba_w = int(round(max(0, m['ac_out'] - m['sit_w'])))
    else: h_spotreba_w = int(round(max(0, m['ac_out'] - m['sit_w'])))

    bojler_aktualni_stav = int(round(b_on[0].varValue))
    akce = rozhodovaci_logika(pv_192[0], spotreba_192[0], m['soc'], ceny_192[0])
    
    ted_5min = ted.replace(minute=(ted.minute // 5) * 5)
    aktualni_hodina = ted_5min.hour
    
    surovy_fs = vsechny_fs.get(ted_5min, 0.0) * 1000
    surovy_pvf = vsechny_pvf.get(ted_5min, 0.0) * 1000
    
    fs_korigovany_w = min(surovy_fs * korekce_fs.get(aktualni_hodina, 1.0), KW_PEAK * 1000)
    pvf_korigovany_w = min(surovy_pvf * korekce_pvf.get(aktualni_hodina, 1.0), KW_PEAK * 1000)
    
    n_radek = pd.DataFrame([{
        'Cas': ted.strftime('%d.%m.%Y %H:%M'),
        'Skutecna_Spotreba_W': h_spotreba_w,
        'Odhad_Spotreba_Modelu_W': int(round(spotreba_192[0] * 1000)),
        'Aktualni_import/export_W': str(m['sit_w']).replace('.', ','),
        'Aktualni_AC_Vystup_W': str(m['ac_out']).replace('.', ','),
        'Celkovy_Vykon_Panelu_W': int(m['dc1']+m['dc2']),
        
        'Predpoved_FS_W': int(round(surovy_fs)),
        'Predpoved_PVF_W': int(round(surovy_pvf)),
        
        'Vykon_Baterie_W': int(m['bat_p']),
        'Baterie_SOC_%': str(m['soc']).replace('.', ','),
        'Simulovane_SOC_%': str(round(float(soc_vars[0].varValue), 1)).replace('.', ','),
        'Cena_EUR/MWh': str(round(ceny_192[0], 2)).replace('.', ','),
        'Doporucena_Akce': akce, 'Akce_PuLP': akce,
        'Duvod_PuLP': vygeneruj_duvod_pulp(akce, ceny_192[0], pv_192[0], m['soc']) + (" | Bojler: ZAPNUT" if bojler_aktualni_stav else ""),
        'Skutecny_AC_Vystup_kWh': str(round(m['v_dnes'], 4)).replace('.', ','),
        'Cista_Vyroba_Panelu_kWh': str(round((m['dc1']+m['dc2'])/1000*0.0833, 4)).replace('.', ','),
        'Import_5min_kWh': str(round((abs(m['sit_w'])/1000*0.0833 if m['sit_w']<0 else 0), 4)).replace('.', ','),
        'Export_5min_kWh': str(round((m['sit_w']/1000*0.0833 if m['sit_w']>0 else 0), 4)).replace('.', ','),
        'Denni_Import_kWh': str(round(denni_import_kwh, 2)).replace('.', ','),
        'Denni_Export_kWh': str(round(denni_export_kwh, 2)).replace('.', ','),
        'AC_vyroba_Dnes_kWh': str(m['v_dnes']).replace('.', ','),
        'Spotreba_Celkem_kWh': str(m['s_celkem']).replace('.', ','),
        'Export_Celkem_kWh': str(m['e_celkem']).replace('.', ','),
        'Uceni_Koeficient_FS': str(round(korekce_fs.get(aktualni_hodina, 1.0), 2)).replace('.', ','),
        'Uceni_Koeficient_PVF': str(round(korekce_pvf.get(aktualni_hodina, 1.0), 2)).replace('.', ','),
        
        'Korigovana_Predpoved_FS_W': int(round(fs_korigovany_w)),
        'Korigovana_Predpoved_PVF_W': int(round(pvf_korigovany_w)),
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
