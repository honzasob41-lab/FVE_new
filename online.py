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
            print(f"CHYBA ENTSO-E (Kod {r.status_code}): {r.text[:200]}")
            return ceny

        root = ElementTree.fromstring(r.content)
        if root.tag.endswith('ErrorDocument'):
            chyba = root.find('.//{*}text')
            print(f"CHYBA ENTSO-E XML: {chyba.text if chyba is not None else 'Neznama chyba'}")
            return ceny

        zaznamy = []
        for ts in root.findall('.//{*}TimeSeries'):
            period = ts.find('.//{*}Period')
            if not period: continue
            
            reso_element = period.find('.//{*}resolution')
            krok_minut = 15 if (reso_element is not None and reso_element.text == 'PT15M') else 60
            
            start_dt = pd.to_datetime(period.find('.//{*}start').text)
            for point in period.findall('.//{*}Point'):
                pos = int(point.find('{*}position').text)
                price = float(point.find('{*}price.amount').text)
                cas_local = (start_dt + timedelta(minutes=(pos - 1) * krok_minut)).tz_convert('Europe/Prague').tz_localize(None)
                zaznamy.append({"Cas": cas_local, "Cena": price})
        
        if zaznamy:
            df = pd.DataFrame(zaznamy).set_index("Cas").resample("15min").ffill().reset_index()
            for _, row in df.iterrows():
                ceny[row["Cas"].to_pydatetime()] = row["Cena"]
    except Exception as e: 
        print(f"Kriticka chyba pri cteni ENTSO-E: {e}")
    return ceny

def nacti_predpoved_fs():
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved = {}
    data = None
    
    potrebujeme_stahnout = True
    if os.path.exists(SOUBOR_PREDPOVEDI):
        cas_zmeny = datetime.fromtimestamp(os.path.getmtime(SOUBOR_PREDPOVEDI))
        if datetime.now() - cas_zmeny < timedelta(hours=3):
            potrebujeme_stahnout = False
            
    if potrebujeme_stahnout:
        try:
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                data = r.json()
                with open(SOUBOR_PREDPOVEDI, 'w') as f:
                    json.dump(data, f)
            else:
                print(f"CHYBA FORECAST.SOLAR (Kod {r.status_code}). API limit pravdepodobne vycerpan.")
        except Exception as e:
            print(f"Chyba spojeni Forecast.Solar: {e}")

    if not data and os.path.exists(SOUBOR_PREDPOVEDI):
        try:
            with open(SOUBOR_PREDPOVEDI, 'r') as f:
                data = json.load(f)
        except Exception as e:
            print(f"Chyba pri cteni cache souboru: {e}")

    if not data:
        return predpoved

    try:
        raw_data = []
        for cas_str, wh in data['result']['watt_hours_period'].items():
            cas = pd.to_datetime(cas_str, errors='coerce')
            if pd.isna(cas): continue
            cas = cas.replace(tzinfo=None)
            raw_data.append({"Cas": cas, "Vy
