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
                print(f"SolaX API nevratilo zadna data.")
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
            print(f"SolaX sitova chyba: {e}")
            time.sleep(5)
    return None

def nacti_ceny_entsoe():
    TOKEN_ENTSOE = "680f2687-dd26-443a-81d1-db067ee6b029"
    DOMENA_CZ = "10YCZ-CEPS-----N"
    cas_utc = datetime.now(timezone.utc)
    ceny, data = {}, {}
    
    potrebujeme_stahnout = True
    if os.path.exists(SOUBOR_CENY):
        try:
            with open(SOUBOR_CENY, 'r') as f: data = json.load(f)
            last_dl_str = data.get("_last_download")
            if last_dl_str:
                if datetime.now() - datetime.fromisoformat(last_dl_str) < timedelta(hours=6):
                    potrebujeme_stahnout = False
        except: pass

    if not potrebujeme_stahnout and "ceny" in data:
        return {pd.to_datetime(k).to_pydatetime(): v for k, v in data["ceny"].items()}

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
                df = pd.DataFrame(zaznamy).drop_duplicates(subset=["Cas"]).set_index("Cas").resample("15min").ffill().reset_index()
                for _, row in df.iterrows(): ceny[row["Cas"].to_pydatetime()] = row["Cena"]
                with open(SOUBOR_CENY, 'w') as f:
                    json.dump({"_last_download": datetime.now().isoformat(), "ceny": {k.strftime('%Y-%m-%d %H:%M:%S'): v for k, v in ceny.items()}}, f)
                return ceny
    except: pass
    if "ceny" in data: return {pd.to_datetime(k).to_pydatetime(): v for k, v in data["ceny"].items()}
    return ceny

def nacti_predpoved_fs():
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved, data = {}, None
    potrebujeme_stahnout = True
    if os.path.exists(SOUBOR_PREDPOVEDI):
        try:
            with open(SOUBOR_PREDPOVEDI, 'r') as f: data = json.load(f)
            last_dl = data.get("_last_download")
            if last_dl and datetime.now() - datetime.fromisoformat(last_dl) < timedelta(hours=3):
                potrebujeme_stahnout = False
        except: pass

    if potrebujeme_stahnout:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                data["_last_download"] = datetime.now().isoformat()
                with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(data, f)
            elif data: 
                data["_last_download"] = datetime.now().isoformat()
                with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(data, f)
        except: pass

    if not data or 'result' not in data: return predpoved
    try:
        raw_data = []
        for cas_str, wh in data['result']['watt_hours_period'].items():
            cas = pd.to_datetime(cas_str, errors='coerce')
            if pd.notna(cas): 
                # OPRAVA: Prevod Wh za 15 minut na prumerny vykon v kW (Wh * 4 / 1000)
                raw_data.append({"Cas": cas.replace(tzinfo=None), "Vykon_kW": float(wh) * 0.004})
        if raw_data:
            df = pd.DataFrame(raw_data).drop_duplicates(subset=["Cas"]).set_index("Cas").sort_index()
            df = df.resample("15min").interpolate(method='linear', limit=3).fillna(0.0).reset_index()
            for _, row in df.iterrows(): predpoved[row["Cas"].to_pydatetime()] = max(0.0, float(row["Vykon_kW"]))
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
        wd = dt.weekday()
        return ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek", "Sobota", "Nedele"][wd]
    cilovy_typ = urci_typ(aktualni_cas)
    df_temp['Typ_Dne'] = df_temp['Cas'].apply(urci_typ)
    df_f = df_temp[(df_temp['Cas'].dt.hour == aktualni_cas.hour) & (df_temp['Cas'].dt.minute // 15 == aktualni_cas.minute // 15)]
    df_final = df_f[df_f['Typ_Dne'] == cilovy_typ]
    if df_final['Cas'].dt.date.nunique() < MIN_DNI_PRO_UCENI:
        pracovni = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
        df_final = df_f[df_f['Typ_Dne'].isin(pracovni if cilovy_typ in pracovni else ["Sobota", "Nedele", "Svatek"])]
    if df_final['Cas'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
        return pd.to_numeric(df_final['Skutecna_Spotreba_W'].astype(str).str.replace(',', '.'), errors='coerce').mean() / 1000.0
    return None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    if cena < 0.0: return "ZAPNOUT_BOJLERY_A_NABIJET" if soc < 99 else "ZAPNOUT_BOJLERY"
    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_I_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0: return "NABIJET_SOLAREM"
    elif soc > 20: return "VYBIJET_PRO_DUM"
    return "NORMALNI_PROVOZ"

def vygeneruj_duvod_pulp(akce, cena, pv, soc):
    if cena < 0.0: return f"Zaporna cena ({cena:.2f} EUR). Nucena spotreba."
    if akce == "NABIJET_ZE_SITE": return f"Levny nakup ({cena:.2f} EUR) pro spicku."
    if akce == "POKRYT_Z_BATERIE": return f"Uspesne vyhnuti se cene {cena:.2f} EUR."
    if akce == "PRODAVAT_DO_SITE": return f"Vyhodny prodej ({cena:.2f} EUR)."
    return "Bezny provoz EMS."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, microsecond=0)
    ted_ctvrthodina = ted.replace(minute=(ted.minute // 15) * 15, second=0)
    
    vsechny_soubory = glob.glob("fve_historie_*.csv")
    df_list = []
    for f in vsechny_soubory:
        try: df_list.append(pd.read_csv(f, sep=';', decimal=','))
        except: pass
        
    if df_list:
        df_h = pd.concat(df_list, ignore_index=True)
        df_h['Cas'] = pd.to_datetime(df_h['Cas'], format='mixed', errors='coerce')
        df_h = df_h.sort_values(by='Cas').reset_index(drop=True)
    else: df_h = pd.DataFrame()

    vsechny_ceny = nacti_ceny_entsoe()
    vsechny_fs = nacti_predpoved_fs()

    ceny_192, pv_192, spotreba_192, casy_192 = [], [], [], []
    for i in range(192):
        cas_kroku = ted_ctvrthodina + timedelta(minutes=15 * i)
        casy_192.append(cas_kroku)
        pv_192.append(vsechny_fs.get(cas_kroku, 0.0))
        ceny_192.append(vsechny_ceny.get(cas_kroku, 0.0))
        spot = nauc_se_spotrebu(df_h, cas_kroku)
        spotreba_192.append(spot if spot is not None else 0.0)

    p_soc = bezpecny_float(df_h.iloc[-1]['Baterie_SOC_%']) if not df_h.empty else 50.0
    model = pulp.LpProblem("EMS", pulp.LpMinimize)
    p_nakup = pulp.LpVariable.dicts("Nakup", range(192), lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", range(192), lowBound=0)
    p_nab = pulp.LpVariable.dicts("Nab", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vyb = pulp.LpVariable.dicts("Vyb", range(192), lowBound=0, upBound=MAX_VYKON_STRIDACE)
    soc = pulp.LpVariable.dicts("SOC", range(192), lowBound=10.0, upBound=100.0)
    is_chg = pulp.LpVariable.dicts("IsChg", range(192), cat=pulp.LpBinary)

    for i in range(192):
        model += (pv_192[i] + p_nakup[i] + p_vyb[i] == spotreba_192[i] + p_prodej[i] + p_nab[i])
        zmena = ((p_nab[i] - p_vyb[i]) * 0.25 / KAPACITA_BATERIE_KWH) * 100.0
        model += soc[i] == (p_soc if i==0 else soc[i-1]) + zmena
        model += p_nab[i] <= MAX_VYKON_STRIDACE * is_chg[i]
        model += p_vyb[i] <= MAX_VYKON_STRIDACE * (1 - is_chg[i])

    model += pulp.lpSum([(p_nakup[i]*(ceny_192[i]+60) - p_prodej[i]*(ceny_192[i]-10))*0.25 for i in range(192)])
    model.solve(pulp.PULP_CBC_CMD(msg=False))

    plan_data = []
    for i in range(192):
        plan_ted = rozhodovaci_logika(pv_192[i], spotreba_192[i], soc[i].varValue, ceny_192[i])
        plan_data.append({
            'Datum': casy_192[i].strftime('%Y-%m-%d'), 
            'Cas': casy_192[i].strftime('%H:%M'),
            'Predpoved_FS_W': int(round(pv_192[i] * 1000)),              
            'Odhad_Spotreba_W': int(round(spotreba_192[i] * 1000)) if spotreba_192[i] > 0 else "Nedostatek dat",
            'Cena_EUR/MWh': str(round(ceny_192[i], 2)).replace('.', ','),
            'Simulovane_SOC_%': str(round(soc[i].varValue, 1)).replace('.', ','),
            'Akce_EMS': plan_ted
        })
    pd.DataFrame(plan_data).to_csv(SOUBOR_PLAN, index=False, sep=';')

    m = nacti_solax_v2()
    if not m: return

    delta_h = 0.25
    if not df_h.empty:
        diff = (ted - df_h.iloc[-1]['Cas']).total_seconds()
        if 0 < diff <= 3600: delta_h = diff / 3600.0

    if m['sit_w'] < 0: 
        h_imp, h_exp = (abs(m['sit_w'])/1000.0)*delta_h, 0.0
    else: 
        h_imp, h_exp = 0.0, (m['sit_w']/1000.0)*delta_h

    h_spot = max(0, m['ac_out'] - m['sit_w'])
    plan_ted = rozhodovaci_logika(pv_192[0], spotreba_192[0], m['soc'], ceny_192[0])
    
    n_radek = pd.DataFrame([{
        'Cas': ted,
        'Skutecna_Spotreba_W': int(round(h_spot)),
        'Aktualni_import/export_W': m['sit_w'],
        'Aktualni_AC_Vystup_W': m['ac_out'],
        'Celkovy_Vykon_Panelu_W': m['dc1']+m['dc2'],
        'Vykon_Baterie_W': m['bat_p'],
        'Odhad_Spotreba_Modelu_W': int(round(spotreba_192[0] * 1000)) if spotreba_192[0] > 0 else "Nedostatek dat",
        'Predpoved_FS_W': int(round(pv_192[0] * 1000)),
        'Baterie_SOC_%': m['soc'],
        'Simulovane_SOC_%': soc[0].varValue,
        'Cena_EUR/MWh': ceny_192[0],
        'Doporucena_Akce': plan_ted,
        'Akce_PuLP': plan_ted,
        'Duvod_PuLP': vygeneruj_duvod_pulp(plan_ted, ceny_192[0], pv_192[0], m['soc']),
        'Skutecny_AC_Vystup_kWh': round(m['v_dnes'], 4),
        'Cista_Vyroba_Panelu_kWh': round((m['dc1']+m['dc2'])/1000*delta_h, 4),
        'Import_5min_kWh': round(h_imp, 4),
        'Export_5min_kWh': round(h_exp, 4),
        'Denni_Import_kWh': 0, 
        'Denni_Export_kWh': 0,
        'AC_vyroba_Dnes_kWh': m['v_dnes'],
        'Spotreba_Celkem_kWh': m['s_celkem'],
        'Export_Celkem_kWh': m['e_celkem']
    }])

    pozadovane_poradi = [
        'Cas', 'Skutecna_Spotreba_W', 'Odhad_Spotreba_Modelu_W', 'Aktualni_import/export_W', 'Aktualni_AC_Vystup_W', 
        'Celkovy_Vykon_Panelu_W', 'Predpoved_FS_W', 'Vykon_Baterie_W', 
        'Baterie_SOC_%', 'Simulovane_SOC_%', 'Cena_EUR/MWh', 'Doporucena_Akce', 'Akce_PuLP', 
        'Duvod_PuLP', 'Skutecny_AC_Vystup_kWh', 'Cista_Vyroba_Panelu_kWh', 'Import_5min_kWh', 
        'Export_5min_kWh', 'Denni_Import_kWh', 'Denni_Export_kWh', 'AC_vyroba_Dnes_kWh', 
        'Spotreba_Celkem_kWh', 'Export_Celkem_kWh'
    ]
    
    n_radek = n_radek[pozadovane_poradi]
    aktualni_soubor = f"fve_historie_{ted.strftime('%Y_%m')}.csv"
    n_radek.to_csv(aktualni_soubor, mode='a', header=not os.path.exists(aktualni_soubor), index=False, sep=';')

if __name__ == "__main__":
    try: main()
    except: traceback.print_exc()
