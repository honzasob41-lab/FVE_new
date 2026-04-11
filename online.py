def nacti_predpoved_fs():
    url = f"https://api.forecast.solar/estimate/{LAT}/{LON}/{DECLINATION}/{AZIMUTH}/{KW_PEAK}"
    predpoved = {}
    data = None
    stara_data = None # Tady si schovame starou cache pro pripad nouze
    
    if os.path.exists(SOUBOR_PREDPOVEDI):
        try:
            with open(SOUBOR_PREDPOVEDI, 'r') as f: 
                stara_data = json.load(f)
                # Pokud je mladsi nez 3 hodiny, pouzijeme rovnou
                if datetime.now() - datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")) <= timedelta(hours=3):
                    data = stara_data
        except: pass
        
    # Pokud nemame data (bud neexistuji, nebo jsou stara), zkusime stahnout nova
    if not data:
        try:
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                data["_last_download"] = datetime.now().isoformat()
                with open(SOUBOR_PREDPOVEDI, 'w') as f: json.dump(data, f)
            else:
                print(f"FS API Error {r.status_code}: {r.text}")
                # SERVER SELHAL -> POUZIJEME STAROU CACHE JAKO ZACHRANU!
                if stara_data: 
                    print("Pouzivam starou FS cache jako zachranu.")
                    data = stara_data
        except Exception as e:
            print(f"FS Connection Error: {e}")
            if stara_data: data = stara_data
            
    if not data or 'result' not in data: return predpoved
    
    try:
        raw_data = []
        for cas_str, w in data['result']['watts'].items():
            cas = pd.to_datetime(cas_str).replace(tzinfo=None)
            raw_data.append({"Cas": cas, "W": float(w)})
            
        if raw_data:
            df = pd.DataFrame(raw_data).set_index("Cas")
            unikatni_dny = df.index.normalize().unique()
            for den in unikatni_dny:
                ranni_cas = den + pd.Timedelta(hours=4)
                vecerni_cas = den + pd.Timedelta(hours=21)
                if ranni_cas not in df.index: df.loc[ranni_cas] = 0.0
                if vecerni_cas not in df.index: df.loc[vecerni_cas] = 0.0
                
            df = df.sort_index().resample("5min").mean().interpolate(method='linear').fillna(0.0)
            for c, r in df.iterrows():
                predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: traceback.print_exc()
    return predpoved

def nacti_predpoved_pvf():
    url = f"https://www.pvforecast.cz/api/?key=8slpgw&lat={LAT}&lon={LON}&format=json"
    predpoved = {}
    data = None
    stara_data = None
    
    if os.path.exists(SOUBOR_PREDPOVEDI_PVF):
        try:
            with open(SOUBOR_PREDPOVEDI_PVF, 'r') as f: 
                stara_data = json.load(f)
                if datetime.now() - datetime.fromisoformat(stara_data.get("_last_download", "2000-01-01")) <= timedelta(hours=3):
                    data = stara_data
        except: pass
        
    if not data:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200:
                try: raw_json = r.json()
                except: raw_json = json.loads(r.text)
                data = {"_last_download": datetime.now().isoformat(), "forecast": raw_json}
                with open(SOUBOR_PREDPOVEDI_PVF, 'w') as f: json.dump(data, f)
            else:
                print(f"PV Forecast API Error {r.status_code}: {r.text}")
                # ZACHRANA ZE STARE CACHE
                if stara_data:
                    print("Pouzivam starou PVF cache jako zachranu.")
                    data = stara_data
        except Exception as e:
            print(f"PV Forecast Connection Error: {e}")
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
            unikatni_dny = df.index.normalize().unique()
            for den in unikatni_dny:
                ranni_cas = den + pd.Timedelta(hours=4)
                vecerni_cas = den + pd.Timedelta(hours=21)
                if ranni_cas not in df.index: df.loc[ranni_cas] = 0.0
                if vecerni_cas not in df.index: df.loc[vecerni_cas] = 0.0
                
            df = df.sort_index().resample("5min").mean().interpolate(method='linear').fillna(0.0)
            for c, r in df.iterrows():
                predpoved[c.to_pydatetime()] = r["W"] / 1000.0
    except: traceback.print_exc()
    return predpoved
