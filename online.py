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
            raw_data.append({"Cas": cas, "Vykon_kW": float(wh) / 1000.0})
        
        if raw_data:
            df = pd.DataFrame(raw_data).set_index("Cas").sort_index()
            df = df.resample("15min").interpolate(method='linear', limit=3).fillna(0.0).reset_index()
            
            for _, row in df.iterrows():
                if pd.notna(row["Cas"]):
                    predpoved[row["Cas"].to_pydatetime()] = max(0.0, float(row["Vykon_kW"]))
    except Exception as e: 
        print(f"Kriticka chyba pri cteni Forecast.Solar dat: {e}")
        
    return predpoved

def nauc_se_spotrebu(df_h, aktualni_cas):
    if df_h.empty or 'Skutecna_Spotreba_kWh' not in df_h.columns: 
        return None

    df_h['Cas'] = pd.to_datetime(df_h['Cas'])

    maska_nedavna = df_h['Cas'] >= (aktualni_cas - timedelta(days=90))
    cas_pred_rokem = aktualni_cas - timedelta(days=365)
    maska_lonska = (df_h['Cas'] >= (cas_pred_rokem - timedelta(days=45))) & (df_h['Cas'] <= (cas_pred_rokem + timedelta(days=45)))

    df_h = df_h[maska_nedavna | maska_lonska].copy()

    cz_holidays = holidays.CZ(years=[aktualni_cas.year, aktualni_cas.year - 1])

    def urci_typ_dne(dt):
        if dt.date() in cz_holidays: 
            return "Svatek"
        wd = dt.weekday()
        if wd == 0: return "Pondeli"
        elif wd == 1: return "Utery"
        elif wd == 2: return "Streda"
        elif wd == 3: return "Ctvrtek"
        elif wd == 4: return "Patek"
        elif wd == 5: return "Sobota"
        elif wd == 6: return "Nedele"

    cilovy_typ = urci_typ_dne(aktualni_cas)
    df_h['Typ_Dne'] = df_h['Cas'].apply(urci_typ_dne)

    df_f = df_h[
        (df_h['Cas'].dt.hour == aktualni_cas.hour) & 
        (df_h['Cas'].dt.minute // 15 == aktualni_cas.minute // 15) & 
        (df_h['Typ_Dne'] == cilovy_typ)
    ].dropna(subset=['Skutecna_Spotreba_kWh'])

    pocet_dostupnych_dni = df_f['Cas'].dt.date.nunique()

    if pocet_dostupnych_dni < MIN_DNI_PRO_UCENI:
        pracovni_dny = ["Pondeli", "Utery", "Streda", "Ctvrtek", "Patek"]
        vikend = ["Sobota", "Nedele"]

        if cilovy_typ in pracovni_dny:
            df_f = df_h[
                (df_h['Cas'].dt.hour == aktualni_cas.hour) & 
                (df_h['Cas'].dt.minute // 15 == aktualni_cas.minute // 15) & 
                (df_h['Typ_Dne'].isin(pracovni_dny))
            ].dropna(subset=['Skutecna_Spotreba_kWh'])
        elif cilovy_typ in vikend or cilovy_typ == "Svatek":
            df_f = df_h[
                (df_h['Cas'].dt.hour == aktualni_cas.hour) & 
                (df_h['Cas'].dt.minute // 15 == aktualni_cas.minute // 15) & 
                (df_h['Typ_Dne'].isin(vikend + ["Svatek"]))
            ].dropna(subset=['Skutecna_Spotreba_kWh'])

    if df_f['Cas'].dt.date.nunique() >= MIN_DNI_PRO_UCENI:
        cisty_sloupec = pd.to_numeric(df_f['Skutecna_Spotreba_kWh'].astype(str).str.replace(',', '.'), errors='coerce')
        return cisty_sloupec.mean() * 12

    return None

def rozhodovaci_logika(prum_p, spot, soc, cena):
    if spot is None: return "UCENI_V_PRUBEHU"
    
    if cena < 0.0:
        if soc < 99.0:
            return "ZAPNOUT_BOJLERY_A_NABIJET"
        else:
            return "ZAPNOUT_BOJLERY"

    bilance = prum_p - spot
    if cena < 1.0 and soc < 20 and bilance <= 0: return "NABIJET_ZE_SITE"
    elif cena > 4.0 and soc > 80: return "PRODAVAT_I_BATERII" if bilance > 0 else "POKRYT_Z_BATERIE"
    elif bilance > 0 and soc > 95: return "PRODAVAT_DO_SITE"
    elif bilance > 0 and soc <= 95: return "NABIJET_SOLAREM"
    elif bilance < 0 and soc > 20: return "VYBIJET_PRO_DUM"
    
    return "NORMALNI_PROVOZ"

def vygeneruj_duvod_pulp(akce, cena, pv_vykon, soc):
    if cena < 0.0:
        if "BOJLERY" in akce:
            return f"Zaporna cena ({cena:.2f} EUR). Nucene zapnuti bojleru k pohlceni energie."
        return f"Zaporna cena ({cena:.2f} EUR). Blokace prodeje a odesilani do site."

    if akce == "NABIJET_ZE_SITE":
        return f"Priprava na budouci spicku (nakup za aktualni cenu {cena:.2f} EUR)."
    elif akce == "NABIJET_SOLAREM":
        return "Ukladani solarnich prebytku pro pozdejsi vyuziti (budouci uspora)."
    elif akce == "POKRYT_Z_BATERIE":
        return f"Vyhnuti se nakupu energie ze site za cenu {cena:.2f} EUR."
    elif akce == "PRODAVAT_DO_SITE":
        if soc >= 99.0:
            return f"Baterie je plna na 100 %, prodej prebytku za cenu {cena:.2f} EUR."
        if pv_vykon > MAX_VYKON_STRIDACE:
            return f"Nabijeci vykon je na maximu, zbytek pretok do site (cena {cena:.2f} EUR)."
        return f"Vyhodny prodej z baterie kvuli vysoke cene ({cena:.2f} EUR)."
    
    if pv_vykon > 0:
        return "Bezna spotreba kryta primym slunecnim vykonem."
    return "Bezny provoz a cekani na vyhodnejsi podminky."

def main():
    ted = datetime.now(ZoneInfo("Europe/Prague")).replace(tzinfo=None, microsecond=0)
    minuty_15 = (ted.minute // 15) * 15
    ted_ctvrthodina = ted.replace(minute=minuty_15, second=0)
    
    vsechny_soubory = glob.glob("fve_historie_*.csv")
    df_list = []
    
    for soubor in vsechny_soubory:
        try:
            df = pd.read_csv(soubor, sep=';', decimal=',')
            df_list.append(df)
        except Exception as e:
            print(f"Chyba pri cteni {soubor}: {e}")
            
    if df_list:
        df_h = pd.concat(df_list, ignore_index=True)
        if 'Cas' in df_h.columns:
            df_h['Cas'] = pd.to_datetime(df_h['Cas'])
        df_h = df_h.sort_values(by='Cas').reset_index(drop=True)
    else:
        df_h = pd.DataFrame()

    vsechny_ceny = nacti_ceny_entsoe()
    vsechny_fs = nacti_predpoved_fs()

    ceny_192, pv_192, spotreba_192, casy_192 = [], [], [], []
    kroky_15min = range(192) 
    
    for offset_i in kroky_15min:
        aktualni_cas_planu = ted_ctvrthodina + timedelta(minutes=15 * offset_i)
        casy_192.append(aktualni_cas_planu)
        
        pv_192.append(vsechny_fs.get(aktualni_cas_planu, 0.0))
        ceny_192.append(vsechny_ceny.get(aktualni_cas_planu, 0.0))
        
        spot = nauc_se_spotrebu(df_h, aktualni_cas_planu)
        spotreba_192.append(spot if spot is not None else 0.0)

    pocatecni_soc = 50.0
    if not df_h.empty and 'Baterie_SOC_%' in df_h.columns:
        pocatecni_soc = bezpecny_float(df_h.iloc[-1]['Baterie_SOC_%'])

    model = pulp.LpProblem("Optimalizace_FVE_15min", pulp.LpMinimize)

    p_nakup = pulp.LpVariable.dicts("Nakup", kroky_15min, lowBound=0)
    p_prodej = pulp.LpVariable.dicts("Prodej", kroky_15min, lowBound=0)
    p_nabijeni = pulp.LpVariable.dicts("Nabijeni", kroky_15min, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    p_vybijeni = pulp.LpVariable.dicts("Vybijeni", kroky_15min, lowBound=0, upBound=MAX_VYKON_STRIDACE)
    soc = pulp.LpVariable.dicts("SOC", kroky_15min, lowBound=10.0, upBound=100.0)

    DELTA_T = 0.25 

    POPLATEK_DISTRIBUCE_NAKUP_EUR = 60.0  
    MARZE_OBCHODNIKA_PRODEJ_EUR = 10.0    

    model += pulp.lpSum([
        (p_nakup[i] * (ceny_192[i] + POPLATEK_DISTRIBUCE_NAKUP_EUR) * DELTA_T) - 
        (p_prodej[i] * (ceny_192[i] - MARZE_OBCHODNIKA_PRODEJ_EUR) * DELTA_T) 
        for i in kroky_15min
    ])

    for i in kroky_15min:
        model += (pv_192[i] + p_nakup[i] + p_vybijeni[i] == spotreba_192[i] + p_prodej[i] + p_nabijeni[i])
        zmena_soc = ((p_nabijeni[i] - p_vybijeni[i]) * DELTA_T / KAPACITA_BATERIE_KWH) * 100.0
        if i == 0:
            model += soc[i] == pocatecni_soc + zmena_soc
        else:
            model += soc[i] == soc[i-1] + zmena_soc

    model.solve(pulp.PULP_CBC_CMD(msg=False))

    plan_data = []
    for i in kroky_15min:
        aktualni_cas_planu = casy_192[i]
        nab_val = p_nabijeni[i].varValue
        vyb_val = p_vybijeni[i].varValue
        nak_val = p_nakup[i].varValue
        cena_v_tomto_kroku = ceny_192[i]
        
        akce = "NORMALNI_PROVOZ"
        
        if cena_v_tomto_kroku < 0.0:
            if soc[i].varValue < 100.0:
                akce = "ZAPNOUT_BOJLERY_A_NABIJET"
            else:
                akce = "ZAPNOUT_BOJLERY"
        else:
            if nab_val > 0.1 and nak_val > 0.1: akce = "NABIJET_ZE_SITE"
            elif nab_val > 0.1: akce = "NABIJET_SOLAREM"
            elif vyb_val > 0.1: akce = "POKRYT_Z_BATERIE"
            elif p_prodej[i].varValue > 0.1: akce = "PRODAVAT_DO_SITE"

        plan_data.append({
            'Datum': aktualni_cas_planu.strftime('%Y-%m-%d'), 
            'Cas': aktualni_cas_planu.strftime('%H:%M'),
            'Predpoved_FS_kWh': str(round(pv_192[i], 2)).replace('.', ','),              
            'Odhad_Spotreba_kW': str(round(spotreba_192[i], 2)).replace('.', ',') if spotreba_192[i] > 0 else "Nedostatek dat",
            'Cena_EUR/MWh': str(round(cena_v_tomto_kroku, 2)).replace('.', ','),
            'Simulovane_SOC_%': str(round(soc[i].varValue, 1)).replace('.', ','),
            'Akce_EMS': akce,
            'Duvod_Akce': vygeneruj_duvod_pulp(akce, cena_v_tomto_kroku, pv_192[i], soc[i].varValue)
        })
        
    pd.DataFrame(plan_data).to_csv(SOUBOR_PLAN, index=False, sep=';')

    m = nacti_solax_v2()
    if not m: 
        print("Skript se ukoncuje: Data ze SolaXu nesla nacist ani po 3 pokusech.")
        return

    h_vyroba = m['v_dnes']
    h_spotreba = 0.0
    h_export = 0.0
    h_import = 0.0
    delta_h = 0.25
    
    if not df_h.empty:
        posledni_zaznam = df_h.iloc[-1]
        rozdil_sekund = (ted - posledni_zaznam['Cas']).total_seconds()
        if 0 < rozdil_sekund <= 3600:
            delta_h = rozdil_sekund / 3600.0

        if 'Spotreba_Celkem_kWh' in posledni_zaznam.index:
            stara_spotreba = bezpecny_float(posledni_zaznam['Spotreba_Celkem_kWh'])
            h_spotreba = max(0.0, m['s_celkem'] - stara_spotreba)
        
        if 'Export_Celkem_kWh' in posledni_zaznam.index:
            stary_export = bezpecny_float(posledni_zaznam['Export_Celkem_kWh'])
            h_export = max(0.0, m['e_celkem'] - stary_export) if stary_export > 0 else ((m['sit_w'] / 1000.0) * delta_h if m['sit_w'] > 0 else 0.0)
        else:
            h_export = (m['sit_w'] / 1000.0) * delta_h if m['sit_w'] > 0 else 0.0
            
        if m['sit_w'] < 0:
            h_import = (abs(m['sit_w']) / 1000.0) * delta_h

        dnesni_zaznamy = df_h[df_h['Cas'].dt.date == ted.date()]
        if not dnesni_zaznamy.empty:
            stara_vyroba = bezpecny_float(dnesni_zaznamy['AC_vyroba_Dnes_kWh'].iloc[-1])
            h_vyroba = max(0.0, m['v_dnes'] - stara_vyroba)
            
            if 'Export_5min_kWh' in dnesni_zaznamy.columns:
                suma_export = pd.to_numeric(dnesni_zaznamy['Export_5min_kWh'].astype(str).str.replace(',', '.'), errors='coerce').sum()
                denni_export = suma_export + h_export
            else:
                denni_export = h_export
                
            if 'Import_5min_kWh' in dnesni_zaznamy.columns:
                suma_import = pd.to_numeric(dnesni_zaznamy['Import_5min_kWh'].astype(str).str.replace(',', '.'), errors='coerce').sum()
                denni_import = suma_import + h_import
            else:
                denni_import = h_import
        else:
            denni_export = h_export
            denni_import = h_import
    else:
        if m['sit_w'] > 0: h_export = (m['sit_w'] / 1000.0) * delta_h
        if m['sit_w'] < 0: h_import = (abs(m['sit_w']) / 1000.0) * delta_h
        denni_export = h_export
        denni_import = h_import

    celkovy_dc_vykon_w = m['dc1'] + m['dc2']
    cista_vyroba_pv_kwh = (celkovy_dc_vykon_w / 1000.0) * delta_h
    o_spot = nauc_se_spotrebu(df_h, ted_ctvrthodina)
    
    fs_now = vsechny_fs.get(ted_ctvrthodina, 0.0)
    cena_h = vsechny_ceny.get(ted_ctvrthodina, 0.0)

    aktualni_akce_pulp = plan_data[0]['Akce_EMS'] if plan_data else "NEDOSTUPNE"
    simulovane_soc_ted = plan_data[0]['Simulovane_SOC_%'] if plan_data else "0,0"
    odhad_spotreby_ted = plan_data[0]['Odhad_Spotreba_kW'] if plan_data else "Nedostatek dat"

    n_radek = pd.DataFrame([{
        'Cas': ted, 
        'Skutecny_AC_Vystup_kWh': str(round(h_vyroba, 4)).replace('.', ','), 
        'Skutecna_Spotreba_kWh': str(round(h_spotreba, 4)).replace('.', ','),
        'Odhad_Spotreba_Modelu_kW': odhad_spotreby_ted,
        'Aktualni_import/export_W': str(m['sit_w']).replace('.', ','),
        'Import_5min_kWh': str(round(h_import, 4)).replace('.', ','),                
        'Export_5min_kWh': str(round(h_export, 4)).replace('.', ','),                                                    
        'Celkovy_Vykon_Panelu_W': str(celkovy_dc_vykon_w).replace('.', ','), 
        'Cista_Vyroba_Panelu_kWh': str(round(cista_vyroba_pv_kwh, 4)).replace('.', ','),
        'Aktualni_AC_Vystup_W': str(m['ac_out']).replace('.', ','),
        'Vykon_Baterie_W': str(m['bat_p']).replace('.', ','), 
        'Baterie_SOC_%': str(m['soc']).replace('.', ','), 
        'Simulovane_SOC_%': simulovane_soc_ted,
        'Cena_EUR/MWh': str(round(cena_h, 2)).replace('.', ','),
        'Predpoved_FS_kWh': str(round(fs_now, 2)).replace('.', ','),                
        'Doporucena_Akce': rozhodovaci_logika(fs_now, o_spot, m['soc'], cena_h),
        'Akce_PuLP': aktualni_akce_pulp,
        'Duvod_PuLP': vygeneruj_duvod_pulp(aktualni_akce_pulp, cena_h, fs_now, m['soc']),
        'Denni_Import_kWh': str(round(denni_import, 2)).replace('.', ','),          
        'Denni_Export_kWh': str(round(denni_export, 2)).replace('.', ','),  
        'AC_vyroba_Dnes_kWh': str(m['v_dnes']).replace('.', ','), 
        'Spotreba_Celkem_kWh': str(m['s_celkem']).replace('.', ','),
        'Export_Celkem_kWh': str(m['e_celkem']).replace('.', ',')                   
    }])

    aktualni_mesic_soubor = f"fve_historie_{ted.strftime('%Y_%m')}.csv"
    vlozit_hlavicku = not os.path.exists(aktualni_mesic_soubor)

    n_radek.to_csv(aktualni_mesic_soubor, mode='a', header=vlozit_hlavicku, index=False, sep=';')
    print(f"Zapis do mesicni historie ({aktualni_mesic_soubor}) uspesne dokoncen!")

if __name__ == "__main__":
    try: main()
    except Exception as e: traceback.print_exc()
