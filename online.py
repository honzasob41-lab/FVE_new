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

warnings.simplefilter(action='ignore', category=UserWarning)

TOKEN_SOLAX = os.environ.get("TOKEN_SOLAX")
WIFI_SN = os.environ.get("SN")

LAT, LON = "49.848", "18.409"
DECLINATION, AZIMUTH = "35", "-50"
KW_PEAK = 10.0

SOUBOR_HISTORIE = "fve_inteligentni_rizeni.csv"
SOUBOR_PLAN = "denni_plan.csv"
SOUBOR_PREDPOVEDI = "predpoved_cache.json"
MIN_DNI_PRO_UCENI = 5
MAX_VYKON_STRIDACE = 10.0
KAPACITA_BATERIE_KWH = 10.0 

def bezpecny_float(val):
    try:
        if pd.isna(val): return 0.0
        if isinstance(val, str):
            return float(val.replace(' ', '').replace(',', '.'))
        return float(val)
    except:
        return 0.0

def nacti_solax_v2():
    url = "https://global.solaxcloud.com/proxyApp/proxy/api/v2/dataAccess/realtimeInfo/get"
    payload = {"wifiSn": WIFI_SN}
    headers = {"tokenId": TOKEN_SOLAX, "Content-Type": "application/json"}
    
    for pokus in range(3):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=15)
            data = r.json()
            if data.get("success") is not True: 
                print(f"SolaX API zamitlo pozadavek (Pokus {pokus+1}/3): {data}")
                time.sleep(5)
                continue
            
            res = data.get("result")
            if not res: 
                print(f"SolaX API nevratilo zadna data (Pokus {pokus+1}/3).")
                time.sleep(5)
                continue
                
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
        except Exception as e: 
            print(f"SolaX sitova chyba (Pokus {pokus+1}/3): {e}")
            time.sleep(5)
    return None

def nacti_ceny_entsoe():
    TOKEN_ENTSOE = "680f2687-dd26-443a-81d1-db067ee6b029"
    DOMENA_CZ = "10YCZ-CEPS-----N"
    cas_utc = datetime.now(timezone.utc)
    start = (cas_utc - timedelta(days=1)).strftime("%Y%m%d0000")
    stop = (cas_utc + timedelta(days=3)).strftime("%Y%m%d0000")
    url = f"https://web-api.tp.entsoe.eu/api?securityToken={TOKEN_ENTSOE}&documentType=A44&in_Domain={DOMENA_CZ}&out_Domain={DOMENA_CZ}&periodStart={start}&periodEnd={stop}"
    ceny = {}
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return ceny
        root = ElementTree.fromstring(r.content)
        zaznamy = []
        for ts in root.findall('.//{*}TimeSeries'):
            period = ts.find('.//{*}Period')
            if not period: continue
            reso = period.find('.//{*}resolution').text
            krok = 15 if reso == 'PT15M' else 60
            start_dt = pd.to_datetime(period.find('.//{*}start').text)
            for point in period.findall('.//{*}Point'):
                pos = int(point.find('{*}position').text)
                price = float(point.find('{*}price.amount').text)
                cas_local = (start_dt + timedelta(minutes=(pos - 1) * krok)).tz_convert('Europe/Prague').tz_localize(None)
                zaznamy.append({"Cas": cas_local, "Cena": price})
        
        if zaznamy:
            # KLÍČOVÁ ZMĚNA: ffill() místo interpolate() pro schodovité ceny
            df = pd.DataFrame(zaznamy).set_index("Cas").resample("15min").ffill().reset_index()
            for _, row in df.iterrows():
                ceny[row["Cas"].to_pydatetime()] = row["Cena"]
    except: pass
    return ceny

def nacti_predpoved_fs():
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved = {}
    data = None
    if os.path.exists(SOUBOR_PREDPOVEDI):
        if datetime.now() - datetime.fromtimestamp(os.path.getmtime(SOUBOR_PREDPOVEDI)) < timedelta(hours=3):
            try:
                with open(SOUBOR_PREDPOVEDI, 'r') as f: data = json.load(f)
            except: pass
    if not data:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(data, f)
        except: pass
    if not data: return predpoved
    try:
        raw = []
        for c_str, wh in data['result']['watt_hours_period'].items():
            cas = pd.to_datetime(c_str).replace(tzinfo=None)
            raw.append({"Cas": cas, "Vykon_kW": float(wh)/1000.0})
        if raw:
            # FVE se linearizovat může (slunce vychází plynule)
            df = pd.DataFrame(raw).set_index("Cas").resample("15min").interpolate(method='linear').fillna(0.0).reset_index()
            for _, row in df.iterrows(): predpoved[row["Cas"].to_pydatetime()] = max(0.0, float(row["Vykon_kW"]))
    except: pass
    return predpoved

def urci_typ_dne(dt):
    cz_hols = holidays.CZ(years=[dt.year, dt.year-1])
    if dt.date() in cz_hols: return "Svatek"
    wd = dt.weekday()
    return ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek", "Sobota", "Nedele"][wd]

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_kWh' not in df_h.columns: return None
    df_h['Cas'] = pd.to_datetime(df_h['Cas'])
    cilovy_typ = urci_typ_dne(aktualni_cas)
    df_h['Typ_Dne'] = df_h['Cas'].apply(urci_typ_dne)
    
    df_f = df_h[(df_h['Cas'].dt.hour == aktualni_cas.hour) & 
                (df_h['Cas'].dt.minute // 15 == aktualni_cas.minute // 15) & 
                (df_h['Typ_Dne'] == cilovy_typ)].dropna(subset=['Skutecna_Spotreba_kWh'])
    
    if df_f['Cas'].dt.date.nunique() < MIN_DNI_PRO_UCENI:
        pracovni = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
        vikend = ["Sobota", "Nedele", "Svatek"]
        skupina = pracovni if cilovy_typ in pracovni else vikend
        df_f = df_h[(df_h['Cas'].dt.hour == aktualni_cas.hour) & 
                    (df_h['Cas'].dt.minute // 15 == aktualni_cas.minute // 15) & 
                    (df_h['Typ_Dne'].isin(skupina))].dropna(subset=['Skutecna_Spotreba_kWh'])

    if df_f['Cas'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
        return pd.to_numeric(df_f['Skutecna_Spotreba_kWh'].astype(str).str.replace(',', '.'), errors='coerce').mean() * 12
    return None

def vygeneruj_duvod_pulp(akce, cena, pv, soc):
    if akce == "NABIJET_ZE_SITE": return f"Vyhodna cena {cena:.2f} EUR/MWh pro pozdejsi spicku."
    if akce == "NABIJET_SOLAREM": return "Ukladani prebytku do baterie."
    if akce == "POKRYT_Z_BATERIE": return f"Vysoka cena {cena:.2f} EUR/MWh, setrime nakup."
    if akce == "PRODAVAT_DO_SITE": return f"Optimalni prodej za {cena:.2f} EUR/MWh."
    return "Bezny provoz."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, microsecond=0)
    ted_ctvrthodina = ted.replace(minute=(ted.minute // 15) * 15, second=0)
    
    df_h = pd.DataFrame()
    if os.path.exists(SOUBOR_HISTORIE):
        df_h = pd.read_csv(SOUBOR_HISTORIE, sep=';', decimal=',')
        df_h['Cas'] = pd.to_datetime(df_h['Cas'])

    vsechny_ceny = nacti_ceny_entsoe()
    vsechny_fs = nacti_predpoved_fs()

    ceny_192, pv_192, spot_192, casy_192 = [], [], [], []
    for i in range(192):
        c_cas = ted_ctvrthodina + timedelta(minutes=15*i)
        casy_192.append(c_cas)
        pv_192.append(vsechny_fs.get(c_cas, 0.0))
        ceny_192.append(vsechny_ceny.get(c_cas, 0.0))
        s = nauc_se_spotrebu(df_h, c_cas)
        spot_192.append(s if s is not None else 0.0)

    p_soc = bezpecny_float(df_h.iloc[-1]['Baterie_SOC_%']) if not df_h.empty else 50.0
    model = pulp.LpProblem("FVE_Optimal", pulp.LpMinimize)
    kroky = range(192)
    p_nakup = pulp.LpVariable.dicts("Nak", kroky, lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Pro", kroky, lowBound=0)
    p_nab = pulp.LpVariable.dicts("Nab", kroky, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vyb = pulp.LpVariable.dicts("Vyb", kroky, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    soc = pulp.LpVariable.dicts("SOC", kroky, lowBound=10.0, upBound=100.0)

    model += pulp.lpSum([(p_nakup[i] - p_prodej[i]) * ceny_192[i] for i in kroky])
    for i in kroky:
        model += (pv_192[i] + p_nakup[i] + p_vyb[i] == spot_192[i] + p_prodej[i] + p_nab[i])
        zmena = ((p_nab[i] - p_vyb[i]) * 0.25 / KAPACITA_BATERIE_KWH) * 100.0
        model += soc[i] == (p_soc if i == 0 else soc[i-1]) + zmena

    model.solve(pulp.PULP_CBC_CMD(msg=False))

    plan_data = []
    for i in kroky:
        akce = "NORMALNI_PROVOZ"
        if p_nab[i].varValue > 0.1 and p_nakup[i].varValue > 0.1: akce = "NABIJET_ZE_SITE"
        elif p_nab[i].varValue > 0.1: akce = "NABIJET_SOLAREM"
        elif p_vyb[i].varValue > 0.1: akce = "POKRYT_Z_BATERIE"
        elif p_prodej[i].varValue > 0.1: akce = "PRODAVAT_DO_SITE"

        plan_data.append({
            'Datum': casy_192[i].strftime('%Y-%m-%d'), 'Cas': casy_192[i].strftime('%H:%M'),
            'Predpoved_FS_kWh': str(round(pv_192[i], 2)).replace('.', ','),
            'Odhad_Spotreba_kW': str(round(spot_192[i], 2)).replace('.', ',') if spot_192[i]>0 else "Nedostatek dat",
            'Cena_EUR/MWh': str(round(ceny_192[i], 2)).replace('.', ','),
            'Simulovane_SOC_%': str(round(soc[i].varValue, 1)).replace('.', ','),
            'Akce_EMS': akce, 'Duvod': vygeneruj_duvod_pulp(akce, ceny_192[i], pv_192[i], soc[i].varValue)
        })
    pd.DataFrame(plan_data).to_csv(SOUBOR_PLAN, index=False, sep=';')

    m = nacti_solax_v2()
    if not m: return
    
    # Výpočet přírůstků pro historii
    h_vyroba = m['v_dnes']
    h_spot, h_exp, h_imp = 0.0, 0.0, 0.0
    if not df_h.empty:
        last = df_h.iloc[-1]
        h_spot = max(0.0, m['s_celkem'] - bezpecny_float(last['Spotreba_Celkem_kWh']))
        h_exp = max(0.0, m['e_celkem'] - bezpecny_float(last['Export_Celkem_kWh']))
        h_imp = (abs(m['sit_w'])/1000.0)*0.25 if m['sit_w'] < 0 else 0.0

    n_radek = pd.DataFrame([{
        'Cas': ted, 
        'Skutecna_Spotreba_kWh': str(round(h_spot, 4)).replace('.', ','),
        'Odhad_Spotreba_Modelu_kW': plan_data[0]['Odhad_Spotreba_kW'],
        'Baterie_SOC_%': str(m['soc']).replace('.', ','),
        'Simulovane_SOC_%': plan_data[0]['Simulovane_SOC_%'],
        'Cena_EUR/MWh': str(round(ceny_192[0], 2)).replace('.', ','),
        'Predpoved_FS_kWh': str(round(pv_192[0], 2)).replace('.', ','),
        'Akce_PuLP': plan_data[0]['Akce_EMS'],
        'Denni_Import_kWh': str(round(h_imp, 2)).replace('.', ','),
        'Denni_Export_kWh': str(round(h_exp, 2)).replace('.', ','),
        'Spotreba_Celkem_kWh': m['s_celkem'], 'Export_Celkem_kWh': m['e_celkem'], 'AC_vyroba_Dnes_kWh': m['v_dnes']
    }])

    pd.concat([df_h, n_radek]).drop_duplicates(subset=['Cas'], keep='last').to_csv(SOUBOR_HISTORIE, index=False, sep=';')
    print("Hotovo. Zmáčkněte Enter...")
    input()

if __name__ == "__main__":
    try: main()
    except: traceback.print_exc(); input()
