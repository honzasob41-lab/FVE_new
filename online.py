import pulp
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
        if data.get("success") is not True: 
            return None
        res = data.get("result")
        if not res: 
            return None
        return {
            "v_dnes": float(res.get("yieldtoday", 0)),
            "soc": float(res.get("soc", 0)),
            "s_celkem": float(res.get("consumeenergy", 0)),
            "dc1": float(res.get("powerdc1", 0)),
            "dc2": float(res.get("powerdc2", 0)),
            "ac_out": float(res.get("acpower", 0)),
            "bat_p": float(res.get("batPower", 0))
        }
    except Exception: 
        return None

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
    pocet_unikatnich_dni = df_f['Cas'].dt.date.nunique()
    if pocet_unikatnich_dni >= MIN_DNI_PRO_UCENI:
        return df_f['Skutecna_Spotreba_kWh'].mean() * 12
    else:
        return None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_I_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0 and soc <= 95: return "NABIJET_SOLAREM"
    elif bilance < 0 and soc > 20: return "VYBIJET_PRO_DUM"
    else: return "NORMALNI_PROVOZ"

def vygeneruj_duvod_pulp(akce, cena, pv_vykon, soc):
    if akce == "NABIJET_ZE_SITE":
        return f"Priprava na budoucí spicku (nakup za aktualni cenu {cena:.2f} Kc)."
    elif akce == "NABIJET_SOLAREM":
        return "Ukladani solarnich prebytku pro pozdejsi vyuziti (budouci uspora)."
    elif akce == "POKRYT_Z_BATERIE":
        return f"Vyhnuti se nakupu energie ze site za cenu {cena:.2f} Kc."
    elif akce == "PRODAVAT_DO_SITE":
        if soc >= 99.0:
            return f"Baterie je plna na 100 %, prodej prebytku za cenu {cena:.2f} Kc."
        else:
            return f"Nabijeci vykon je na maximu, zbytek pretok do site (cena {cena:.2f} Kc)."
    else:
        if pv_vykon > 0:
            return "Bezna spotreba kryta primym slunecnim vykonem."
        return "Cekani na vyhodnejsi podminky."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None)
    ted_cela_hodina = ted.replace(minute=0, second=0, microsecond=0)
    
    df_h = pd.DataFrame()
    if os.path.exists(SOUBOR_HISTORIE):
        df_h = pd.read_csv(SOUBOR_HISTORIE, sep=';', decimal=',')
        if not df_h.empty and 'Cas' in df_h.columns:
            df_h['Cas'] = pd.to_datetime(df_h['Cas'])

    dnes_pulnoc = ted_cela_hodina.replace(hour=0)
    zitra_pulnoc = dnes_pulnoc + timedelta(days=1)

    fs_dnes = nacti_predpoved_fs_dnes(dnes_pulnoc)
    pvcz_dnes = nacti_predpoved_pvcz_dnes(dnes_pulnoc)
    ceny_dnes = nacti_ceny_entsoe_dnes(dnes_pulnoc)
    
    fs_zitra = nacti_predpoved_fs_dnes(zitra_pulnoc)
    pvcz_zitra = nacti_predpoved_pvcz_dnes(zitra_pulnoc)
    ceny_zitra = nacti_ceny_entsoe_dnes(zitra_pulnoc)

    ceny_48, pv_48, spotreba_48, casy_48 = [], [], [], []
    for offset_h in range(48):
        aktualni_hodina_planu = dnes_pulnoc + timedelta(hours=offset_h)
        h = aktualni_hodina_planu.hour
        casy_48.append(aktualni_hodina_planu)
        
        if offset_h < 24:
            fs_val, pvcz_val, cena = fs_dnes.get(h, 0.0), pvcz_dnes.get(h, 0.0), ceny_dnes.get(h, 0.0)
        else:
            fs_val, pvcz_val, cena = fs_zitra.get(h, 0.0), pvcz_zitra.get(h, 0.0), ceny_zitra.get(h, 0.0)
            
        pv_48.append((fs_val + pvcz_val) / 2)
        ceny_48.append(cena)
        
        spot = nauc_se_spotrebu(df_h, aktualni_hodina_planu)
        spotreba_48.append(spot if spot is not None else 0.0)

    KAPACITA_BATERIE_KWH = 10.0 
    MAX_VYKON_DOBIJENI = 6.0    
    pocatecni_soc = 50.0
    if not df_h.empty and 'Baterie_SOC_%' in df_h.columns:
        pocatecni_soc = float(df_h.iloc[-1]['Baterie_SOC_%'])

    model = pulp.LpProblem("Optimalizace_FVE", pulp.LpMinimize)
    hodiny = range(48)

    p_nakup = pulp.LpVariable.dicts("Nakup", hodiny, lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", hodiny, lowBound=0)
    p_nabijeni = pulp.LpVariable.dicts("Nabijeni", hodiny, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vybijeni = pulp.LpVariable.dicts("Vybijeni", hodiny, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    soc = pulp.LpVariable.dicts("SOC", hodiny, lowBound=10.0, upBound=100.0)

    # UPDATED: Odstranen koeficient 0.5, pouziva se cista cena z ENTSO-E
    model += pulp.lpSum([p_nakup[h] * ceny_48[h] - p_prodej[h] * ceny_48[h] for h in hodiny])

    for h in hodiny:
        model += (pv_48[h] + p_nakup[h] + p_vybijeni[h] == spotreba_48[h] + p_prodej[h] + p_nabijeni[h])
        zmena_soc = ((p_nabijeni[h] - p_vybijeni[h]) / KAPACITA_BATERIE_KWH) * 100.0
        if h == 0:
            model += soc[h] == pocatecni_soc + zmena_soc
        else:
            model += soc[h] == soc[h-1] + zmena_soc

    model.solve(pulp.PULP_CBC_CMD(msg=False))

    plan_data = []
    for h in hodiny:
        aktualni_hodina_planu = casy_48[h]
        if aktualni_hodina_planu >= ted_cela_hodina:
            nab_val = p_nabijeni[h].varValue
            vyb_val = p_vybijeni[h].varValue
            nak_val = p_nakup[h].varValue
            
            akce = "NORMALNI_PROVOZ"
            if nab_val > 0.1 and nak_val > 0.1:
                akce = "NABIJET_ZE_SITE"
            elif nab_val > 0.1:
                akce = "NABIJET_SOLAREM"
            elif vyb_val > 0.1:
                akce = "POKRYT_Z_BATERIE"
            elif p_prodej[h].varValue > 0.1:
                akce = "PRODAVAT_DO_SITE"

            duvod_pulp = vygeneruj_duvod_pulp(akce, ceny_48[h], pv_48[h], soc[h].varValue)

            plan_data.append({
                'Datum': aktualni_hodina_planu.strftime('%Y-%m-%d'), 
                'Hodina': f"{aktualni_hodina_planu.hour:02d}:00",
                'Predpoved_FS_kWh': round(fs_dnes.get(aktualni_hodina_planu.hour, 0.0) if h < 24 else fs_zitra.get(aktualni_hodina_planu.hour, 0.0), 2),
                'Predpoved_PVCZ_kWh': round(pvcz_dnes.get(aktualni_hodina_planu.hour, 0.0) if h < 24 else pvcz_zitra.get(aktualni_hodina_planu.hour, 0.0), 2),
                'Predpoved_Prumer_kWh': round(pv_48[h], 2),
                'Odhad_Spotreba_kWh': round(spotreba_48[h], 2) if spotreba_48[h] > 0 else "Nedostatek dat",
                'Cena_CZK_kWh': round(ceny_48[h], 2),
                'Simulovane_SOC_%': round(soc[h].varValue, 1),
                'Akce_EMS': akce,
                'Duvod_Akce': duvod_pulp
            })
        
    pd.DataFrame(plan_data).to_csv(SOUBOR_PLAN, index=False, sep=';', decimal=',')

    m = nacti_solax_v2()
    if not m: return

    h_vyroba = m['v_dnes']
    h_spotreba = 0.0
    delta_h = 0.25
    
    if not df_h.empty:
        posledni_zaznam = df_h.iloc[-1]
        rozdil_sekund = (ted - posledni_zaznam['Cas']).total_seconds()
        if 0 < rozdil_sekund <= 3600:
            delta_h = rozdil_sekund / 3600.0

        h_spotreba = max(0.0, m['s_celkem'] - posledni_zaznam['Spotreba_Celkem_kWh'])
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

    aktualni_akce_pulp = "NEDOSTUPNE"
    for radek_planu in plan_data:
        if radek_planu['Datum'] == ted_cela_hodina.strftime('%Y-%m-%d') and radek_planu['Hodina'] == f"{ted_cela_hodina.hour:02d}:00":
            aktualni_akce_pulp = radek_planu['Akce_EMS']
            break

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
        'Akce_PuLP': aktualni_akce_pulp,
        'Duvod_PuLP': vygeneruj_duvod_pulp(aktualni_akce_pulp, cena_h, p_now, m['soc']),
        'AC_vyroba_Dnes_kWh': m['v_dnes'], 
        'Spotreba_Celkem_kWh': m['s_celkem']
    }])

    pd.concat([df_h, n_radek]).drop_duplicates(subset=['Cas'], keep='last').to_csv(SOUBOR_HISTORIE, index=False, sep=';', decimal=',')

if __name__ == "__main__":
    try: 
        main()
    except Exception as e: 
        print(f"Kriticka chyba pri behu skriptu: {e}")
        traceback.print_exc()
