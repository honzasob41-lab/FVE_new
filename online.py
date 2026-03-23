import requests
import pandas as pd
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import os
import warnings
import traceback
from xml.etree import ElementTree

warnings.simplefilter(action='ignore', category=UserWarning)

TOKEN_SOLAX = os.environ.get("TOKEN_SOLAX")
WIFI_SN = os.environ.get("SN")

LAT, LON = "49.848", "18.409"
DECLINATION, AZIMUTH = "35", "-50"
KW_PEAK = 10.0

SOUBOR_HISTORIE = "fve_inteligentni_rizeni.csv"
SOUBOR_PLAN = "denni_plan.csv"
MIN_DNI_PRO_UCENI = 5

def nacti_solax_v2():
    url = "https://global.solaxcloud.com/proxyApp/proxy/api/v2/dataAccess/realtimeInfo/get"
    payload = {"wifiSn": WIFI_SN}
    headers = {"tokenId": TOKEN_SOLAX, "Content-Type": "application/json"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=15)
        data = r.json()
        if data.get("success") is not True: return None
        res = data.get("result")
        if not res: return None
        return {
            "v_dnes": float(res.get("yieldtoday", 0)),
            "soc": float(res.get("soc", 0)),
            "s_celkem": float(res.get("consumeenergy", 0)),
            "dc1": float(res.get("powerdc1", 0)),
            "dc2": float(res.get("powerdc2", 0)),
            "ac_out": float(res.get("acpower", 0)),
            "bat_p": float(res.get("batPower", 0))
        }
    except: return None

def nacti_ceny_entsoe_dnes(dnesni_datum):
    TOKEN_ENTSOE = "680f2687-dd26-443a-81d1-db067ee6b029"
    DOMENA_CZ = "10YCZ-CEPS-----N"
    cas_utc = datetime.now(timezone.utc)
    start = (cas_utc - timedelta(days=1)).strftime("%Y%m%d0000")
    stop = (cas_utc + timedelta(days=2)).strftime("%Y%m%d0000")
    url = f"https://web-api.tp.entsoe.eu/api?securityToken={TOKEN_ENTSOE}&documentType=A44&in_Domain={DOMENA_CZ}&out_Domain={DOMENA_CZ}&periodStart={start}&periodEnd={stop}"
    ceny = {}
    try:
        r = requests.get(url, timeout=20)
        root = ElementTree.fromstring(r.content)
        zaznamy = []
        for ts in root.findall('.//{*}TimeSeries'):
            period = ts.find('.//{*}Period')
            if not period: continue
            start_dt = pd.to_datetime(period.find('.//{*}start').text)
            for point in period.findall('.//{*}Point'):
                pos = int(point.find('{*}position').text)
                price = float(point.find('{*}price.amount').text)
                cas_local = (start_dt + timedelta(minutes=(pos - 1) * 60)).tz_convert('Europe/Prague').tz_localize(None)
                zaznamy.append({"Cas": cas_local, "Cena": price})
        df = pd.DataFrame(zaznamy).set_index("Cas").resample("h").mean().reset_index()
        for _, row in df.iterrows():
            if row["Cas"].date() == dnesni_datum.date():
                ceny[row["Cas"].hour] = (row["Cena"] * 25.0) / 1000.0
    except: pass
    return ceny

def nacti_predpoved_fs_dnes(dnesni_datum):
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved = {}
    try:
        r = requests.get(url, timeout=10).json()
        for cas_str, wh in r['result']['watt_hours_period'].items():
            cas = pd.to_datetime(cas_str)
            if cas.date() == dnesni_datum.date():
                predpoved[cas.hour] = float(wh) / 1000.0
    except: pass
    return predpoved

def nacti_predpoved_pvcz_dnes(dnesni_datum):
    url = f"http://www.pvforecast.cz/api/?key=8slpgw&lat={LAT}&lon={LON}&format=json"
    predpoved = {}
    try:
        r = requests.get(url, timeout=10).json()
        df = pd.DataFrame(list(r.items()) if isinstance(r, dict) else r)
        df.columns = ['Cas', 'W_m2']
        df['Cas'] = pd.to_datetime(df['Cas'])
        for _, row in df.iterrows():
            if row['Cas'].date() == dnesni_datum.date():
                predpoved[row['Cas'].hour] = (float(row['W_m2']) / 1000.0) * KW_PEAK * 0.8
    except: pass
    return predpoved

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_kWh' not in df_h.columns: return None
    je_vikend = aktualni_cas.weekday() >= 5
    df_h['Cas'] = pd.to_datetime(df_h['Cas'])
    df_f = df_h[(df_h['Cas'].dt.hour == aktualni_cas.hour) & 
                ((df_h['Cas'].dt.weekday >= 5) == je_vikend)].dropna(subset=['Skutecna_Spotreba_kWh'])
    # Matematicka korekce: prumer za 15 minut * 4 = hodinovy odhad
    return (df_f['Skutecna_Spotreba_kWh'].mean() * 4) if len(df_f) >= MIN_DNI_PRO_UCENI else None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_I_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0 and soc <= 95: return "NABIJET_SOLAREM"
    elif bilance < 0 and soc > 20: return "VYBIJET_PRO_DUM"
    else: return "NORMALNI_PROVOZ"

def main():
    # Presny cas pro ukladani s minutami
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, second=0, microsecond=0)
    # Zaokrouhleny cas pro dotazy na API a predpovedi
    ted_cela_hodina = ted.replace(minute=0)
    
    try:
        df_h = pd.read_csv(SOUBOR_HISTORIE, parse_dates=['Cas'], sep=';', decimal=',') if os.path.exists(SOUBOR_HISTORIE) else pd.DataFrame()
    except: df_h = pd.DataFrame()

    datum_p = ted_cela_hodina + timedelta(days=1) if ted_cela_hodina.hour >= 18 else ted_cela_hodina
    fs_p = nacti_predpoved_fs_dnes(datum_p)
    pvcz_p = nacti_predpoved_pvcz_dnes(datum_p)
    ceny_p = nacti_ceny_entsoe_dnes(datum_p)

    plan_data = []
    for h in range(24):
        fs_val = fs_p.get(h, 0.0)
        pvcz_val = pvcz_p.get(h, 0.0)
        p_avg = (fs_val + pvcz_val) / 2
        spot = nauc_se_spotrebu(df_h, datum_p.replace(hour=h))
        plan_data.append({
            'Datum': datum_p.strftime('%Y-%m-%d'), 'Hodina': f"{h:02d}:00",
            'Predpoved_FS_kWh': round(fs_val, 2),
            'Predpoved_PVCZ_kWh': round(pvcz_val, 2),
            'Predpoved_Prumer_kWh': round(p_avg, 2),
            'Odhad_Spotreba_kWh': round(spot, 2) if spot else 0.0,
            'Cena_CZK_kWh': round(ceny_p.get(h, 0.0), 2),
            'Akce_EMS': rozhodovaci_logika(p_avg, spot, 50, ceny_p.get(h, 0.0))
        })
    pd.DataFrame(plan_data).to_csv(SOUBOR_PLAN, index=False, sep=';', decimal=',')

    m = nacti_solax_v2()
    if not m: return

    # Vychozi hodnoty pro diferencialni vypocty
    h_vyroba = m['v_dnes']
    h_spotreba = 0.0
    delta_h = 0.25
    
    if not df_h.empty:
        # Vypocet presneho casoveho kroku v hodinach (numericka integrace)
        posledni_zaznam = df_h.iloc[-1]
        rozdil_sekund = (ted - posledni_zaznam['Cas']).total_seconds()
        if 0 < rozdil_sekund <= 3600:
            delta_h = rozdil_sekund / 3600.0

        # Spotreba domu (vzdalenost od absolutne posledniho zaznamu)
        h_spotreba = max(0.0, m['s_celkem'] - posledni_zaznam['Spotreba_Celkem_kWh'])
        
        # AC Vyroba (vzdalenost jen od dnesnich zaznamu, protoze se o pulnoci nuluje)
        dnesni_zaznamy = df_h[df_h['Cas'].dt.date == ted.date()]
        if not dnesni_zaznamy.empty:
            h_vyroba = max(0.0, m['v_dnes'] - dnesni_zaznamy['AC_vyroba_Dnes_kWh'].iloc[-1])

    celkovy_dc_vykon_w = m['dc1'] + m['dc2']
    cista_vyroba_pv_kwh = (celkovy_dc_vykon_w / 1000.0) * delta_h

    o_spot = nauc_se_spotrebu(df_h, ted_cela_hodina)
    fs_now = nacti_predpoved_fs_dnes(ted_cela_hodina).get(ted_cela_hodina.hour, 0.0)
    pvcz_now = nacti_predpoved_pvcz_dnes(ted_cela_hodina).get(ted_cela_hodina.hour, 0.0)
    p_now = (fs_now + pvcz_now) / 2
    cena_h = nacti_ceny_entsoe_dnes(ted_cela_hodina).get(ted_cela_hodina.hour, 0.0)

    n_radek = pd.DataFrame([{
        'Cas': ted, 
        'Skutecny_AC_Vystup_kWh': round(h_vyroba, 2), 
        'Skutecna_Spotreba_kWh': round(h_spotreba, 2),
        'Celkovy_Vykon_Panelu_W': celkovy_dc_vykon_w, 
        'Cista_Vyroba_Panelu_kWh': round(cista_vyroba_pv_kwh, 4),
        'Aktualni_AC_Vystup_W': m['ac_out'],
        'Vykon_Baterie_W': m['bat_p'], 
        'Baterie_SOC_%': m['soc'], 
        'Cena_CZK_kWh': round(cena_h, 2),
        'Predpoved_FS_kWh': round(fs_now, 2),
        'Predpoved_PVCZ_kWh': round(pvcz_now, 2),
        'Predpoved_Prumer_kWh': round(p_now, 2),
        'Doporucena_Akce': rozhodovaci_logika(p_now, o_spot, m['soc'], cena_h),
        'AC_vyroba_Dnes_kWh': m['v_dnes'], 
        'Spotreba_Celkem_kWh': m['s_celkem']
    }])

    pd.concat([df_h, n_radek]).drop_duplicates(subset=['Cas'], keep='last').to_csv(SOUBOR_HISTORIE, index=False, sep=';', decimal=',')

if __name__ == "__main__":
    try: main()
    except: traceback.print_exc()