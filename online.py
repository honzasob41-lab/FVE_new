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

TOKEN_SOLAX = os.environ.get("TOKEN_SOLAX")
WIFI_SN = os.environ.get("SN")
LAT, LON = "49.848", "18.409"
DECLINATION, AZIMUTH = "35", "-50"
KW_PEAK = 10.0

SOUBOR_PLAN = "denni_plan.csv"
SOUBOR_PREDPOVEDI = "predpoved_cache.json"
SOUBOR_CENY = "ceny_cache.json"
MIN_DNI_PRO_UCENI = 2
MAX_VYKON_STRIDACE = 10.0
KAPACITA_BATERIE_KWH = 10.0

def bezpecny_float(val):
    try:
        if pd.isna(val): return 0.0
        s = str(val).replace(' ', '').replace(',', '.')
        return float(s)
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
    predpoved, data = {}, None
    if os.path.exists(SOUBOR_PREDPOVEDI):
        try:
            with open(SOUBOR_PREDPOVEDI, 'r') as f: data = json.load(f)
            if datetime.now() - datetime.fromisoformat(data.get("_last_download", "2000-01-01")) > timedelta(hours=3):
                data = None
        except: data = None
    if not data:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                data["_last_download"] = datetime.now().isoformat()
                with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(data, f)
        except: pass
    if not data or 'result' not in data: return predpoved
    try:
        for cas_str, wh in data['result']['watt_hours_period'].items():
            cas = pd.to_datetime(cas_str, errors='coerce')
            if pd.notna(cas):
                # OPRAVENO: Wh za periodu (hodinu) odpovidá prumernému výkonu W
                predpoved[cas.replace(tzinfo=None).to_pydatetime()] = float(wh) / 1000.0
    except: pass
    return predpoved

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_W' not in df_h.columns: return None
    df_h['Cas'] = pd.to_datetime(df_h['Cas'], format='mixed', errors='coerce')
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
        prac = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
        df_final = df_f[df_f['Typ_Dne'].isin(prac if cilovy_typ in prac else ["Sobota", "Nedele", "Svatek"])]
    if df_final['Cas'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
        val = pd.to_numeric(df_final['Skutecna_Spotreba_W'].astype(str).str.replace(',', '.'), errors='coerce').mean()
        return max(0.0, val / 1000.0) # POJISTKA PROTI MINUSU
    return None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    if cena < 0.0: return "ZAPNOUT_BOJLERY_A_NABIJET" if soc < 99 else "ZAPNOUT_BOJLERY"
    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_Z_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0: return "NABIJET_SOLAREM"
    elif soc > 20: return "VYBIJET_PRO_DUM"
    return "NORMALNI_PROVOZ"

def vygeneruj_duvod_pulp(akce, cena, pv, soc):
    if cena < 0.0: return f"Zaporna cena ({cena:.2f} EUR). Nucena spotreba."
    if akce == "PRODAVAT_Z_BATERII": return f"Vysoka cena ({cena:.2f} EUR), prodavame prebytek."
    if akce == "POKRYT_Z_BATERIE": return f"Kryti spotreby z baterie pri cene {cena:.2f} EUR."
    return "Bezny provoz EMS."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, microsecond=0)
    ted_ctvrt = ted.replace(minute=(ted.minute // 15) * 15, second=0)
    
    vsechny_soubory = glob.glob("fve_historie_*.csv")
    df_list = [pd.read_csv(f, sep=';', decimal=',') for f in vsechny_soubory]
    df_h = pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()
    if not df_h.empty:
        df_h['Cas'] = pd.to_datetime(df_h['Cas'], format='mixed', errors='coerce')
        df_h = df_h.sort_values(by='Cas').reset_index(drop=True)

    vsechny_ceny = nacti_ceny_entsoe()
    vsechny_fs = nacti_predpoved_fs()

    ceny_192, pv_192, spotreba_192, casy_192 = [], [], [], []
    for i in range(192):
        c = ted_ctvrt + timedelta(minutes=15 * i)
        casy_192.append(c)
        pv_192.append(vsechny_fs.get(c, 0.0))
        ceny_192.append(vsechny_ceny.get(c, 0.0))
        spot = nauc_se_spotrebu(df_h, c)
        spotreba_192.append(spot if spot is not None else 0.0)

    p_soc = bezpecny_float(df_h.iloc[-1]['Baterie_SOC_%']) if not df_h.empty else 50.0
    model = pulp.LpProblem("EMS", pulp.LpMinimize)
    p_nab = pulp.LpVariable.dicts("Nab", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vyb = pulp.LpVariable.dicts("Vyb", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_nakup = pulp.LpVariable.dicts("Nakup", range(192), lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", range(192), lowBound=0)
    soc_vars = pulp.LpVariable.dicts("SOC", range(192), lowBound=10.0, upBound=100.0)
    is_chg = pulp.LpVariable.dicts("IsChg", range(192), cat=pulp.LpBinary)

    for i in range(192):
        model += (pv_192[i] + p_nakup[i] + p_vyb[i] == spotreba_192[i] + p_prodej[i] + p_nab[i])
        zmena = ((p_nab[i] - p_vyb[i]) * 0.25 / KAPACITA_BATERIE_KWH) * 100.0
        model += soc_vars[i] == (p_soc if i==0 else soc_vars[i-1]) + zmena
        model += p_nab[i] <= MAX_VYKON_STRIDACE * is_chg[i]
        model += p_vyb[i] <= MAX_VYKON_STRIDACE * (1 - is_chg[i])
    model += pulp.lpSum([(p_nakup[i]*(ceny_192[i]+60) - p_prodej[i]*(ceny_192[i]-10))*0.25 for i in range(192)])
    model.solve(pulp.PULP_CBC_CMD(msg=False))

    m = nacti_solax_v2()
    if not m: return

    # Vygenerování nového řádku s pevným pořadím
    h_spot = max(0, m['ac_out'] - m['sit_w'])
    akce = rozhodovaci_logika(pv_192[0], spotreba_192[0], m['soc'], ceny_192[0])
    
    n_radek = pd.DataFrame([{
        'Cas': ted.strftime('%Y-%m-%d %H:%M:%S'),
        'Skutecna_Spotreba_W': int(round(h_spot)),
        'Odhad_Spotreba_Modelu_W': int(round(spotreba_192[0] * 1000)),
        'Aktualni_import/export_W': str(m['sit_w']).replace('.', ','),
        'Aktualni_AC_Vystup_W': str(m['ac_out']).replace('.', ','),
        'Celkovy_Vykon_Panelu_W': int(m['dc1']+m['dc2']),
        'Predpoved_FS_W': int(round(pv_192[0] * 1000)),
        'Vykon_Baterie_W': int(m['bat_p']),
        'Baterie_SOC_%': str(m['soc']).replace('.', ','),
        'Simulovane_SOC_%': str(round(float(soc_vars[0].varValue), 1)).replace('.', ','),
        'Cena_EUR/MWh': str(round(ceny_192[0], 2)).replace('.', ','),
        'Doporucena_Akce': akce, 'Akce_PuLP': akce,
        'Duvod_PuLP': vygeneruj_duvod_pulp(akce, ceny_192[0], pv_192[0], m['soc']),
        'Skutecny_AC_Vystup_kWh': str(round(m['v_dnes'], 4)).replace('.', ','),
        'Cista_Vyroba_Panelu_kWh': str(round((m['dc1']+m['dc2'])/1000*0.25, 4)).replace('.', ','),
        'Import_5min_kWh': str(round((abs(m['sit_w'])/1000*0.25 if m['sit_w']<0 else 0), 4)).replace('.', ','),
        'Export_5min_kWh': str(round((m['sit_w']/1000*0.25 if m['sit_w']>0 else 0), 4)).replace('.', ','),
        'Denni_Import_kWh': '0,0', 'Denni_Export_kWh': '0,0',
        'AC_vyroba_Dnes_kWh': str(m['v_dnes']).replace('.', ','),
        'Spotreba_Celkem_kWh': str(m['s_celkem']).replace('.', ','),
        'Export_Celkem_kWh': str(m['e_celkem']).replace('.', ',')
    }])

    poradi = [
        'Cas', 'Skutecna_Spotreba_W', 'Odhad_Spotreba_Modelu_W', 'Aktualni_import/export_W', 
        'Aktualni_AC_Vystup_W', 'Celkovy_Vykon_Panelu_W', 'Predpoved_FS_W', 'Vykon_Baterie_W', 
        'Baterie_SOC_%', 'Simulovane_SOC_%', 'Cena_EUR/MWh', 'Doporucena_Akce', 'Akce_PuLP', 
        'Duvod_PuLP', 'Skutecny_AC_Vystup_kWh', 'Cista_Vyroba_Panelu_kWh', 'Import_5min_kWh', 
        'Export_5min_kWh', 'Denni_Import_kWh', 'Denni_Export_kWh', 'AC_vyroba_Dnes_kWh', 
        'Spotreba_Celkem_kWh', 'Export_Celkem_kWh'
    ]
    
    soubor_hist = f"fve_historie_{ted.strftime('%Y_%m')}.csv"
    n_radek[poradi].to_csv(soubor_hist, mode='a', header=not os.path.exists(soubor_hist), index=False, sep=';', decimal=',')

if __name__ == "__main__":
    try: main()
    except: traceback.print_exc()
