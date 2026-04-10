import requests

def otestuj_pv_forecast():
    # Tvé parametry
    lat = "49.84838"
    lon = "18.40898"
    key = "8slpgw"
    
    # Sestavení adresy (přesně podle starší dokumentace pro GET dotaz)
    url = f"https://www.pvforecast.cz/api/?key={key}&lat={lat}&lon={lon}&format=json"
    
    print(f"1. Zkouším se připojit na adresu:\n{url}\n")
    
    try:
        odpoved = requests.get(url, timeout=10)
        print(f"2. Server odpověděl kódem (200 = OK): {odpoved.status_code}\n")
        
        print("3. Surový text, který server poslal zpět (prvních 500 znaků):")
        text_odpovedi = odpoved.text
        print(text_odpovedi[:500])
        print("-" * 50)
        
        # Zkusíme zjistit, jestli to je seznam dat, nebo chybová zpráva
        if odpoved.status_code == 200:
            data = odpoved.json()
            if isinstance(data, list):
                print(f"\n4. PARSOVÁNÍ: Úspěch! API vrátilo {len(data)} řádků dat.")
                print("Ukázka prvních 3 řádků (Čas, Osvit):")
                for item in data[:3]:
                    print(f" - {item}")
            else:
                print("\n4. PARSOVÁNÍ: Pozor! API sice odpovědělo, ale neposlalo seznam dat.")
                print("Asi je to chybová zpráva. Zkontroluj text výše.")
                
    except Exception as e:
        print(f"\nKritická chyba spojení: {e}")

if __name__ == "__main__":
    otestuj_pv_forecast()
