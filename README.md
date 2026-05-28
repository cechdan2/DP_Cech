# Diplomová práce

## O projektu

Tento repozitář obsahuje zdrojové kódy k diplomové práci zabývající se návrhem a realizací mechatronického automatizovaného nápojového dávkovače s pohyblivým ramenem. Celý systém je vestavěn do fyzického modelu sklízecí mlátičky Bruder John Deere T670i.

Architektura projektu je rozdělena do dvou hlavních vrstev:
- **Nízkoúrovňové řízení (Arduino UNO):** C++ firmware obsluhující hardwarové periferie – IR senzory (38kHz), PWM řízení kapalinového čerpadla, polohování servomotoru a adresovatelné RGB LED podsvícení. Stará se o stavový automat dávkovací sekvence a nouzové stavy (E-STOP).
- **Vysokoúrovňové řízení a HMI (Raspberry Pi):** Backend postavený na Pythonu a mikroframeworku Flask. Zajišťuje sériovou komunikaci s Arduinem (UART s vlastním XOR checksum protokolem), ukládání provozních dat a kalibračních tabulek do SQLite databáze a poskytuje frontendové webové rozhraní.

## Struktura projektu

Tento projekt využívá následující doporučenou strukturu adresářů:

```text
/DP_Cech
 ├── app.py
 ├── ARDUINO.txt
 ├── requirements.txt
 ├── README.md
 ├── LICENSE
 └── templates/
      └── index.html
```

## Instalace a spuštění

### Prerekvizity

- **Hardware:** Raspberry Pi (s OS založeným na Debianu), Arduino UNO.
- **Software:** Python 3.x, pip, Arduino IDE (pro flashnutí firmwaru).

### Příprava hardwaru (Arduino)

1. Otevřete soubor `ARDUINO.txt` (doporučeno uložit s příponou `.ino`) v Arduino IDE.
2. Ujistěte se, že máte nainstalované knihovny:
   - `Servo.h` (součást standardní výbavy Arduino IDE)
   - `Adafruit_NeoPixel`
3. Zkompilujte kód a nahrajte jej do Arduina UNO.
4. Připojte Arduino k Raspberry Pi pomocí USB kabelu (v konfiguraci `app.py` je port nastaven na `/dev/ttyUSB0` pro Linux/Raspberry Pi nebo `COM5` pro Windows).

### Příprava softwaru (Raspberry Pi / Lokální vývoj)

1. Naklonujte repozitář a přejděte do kořenového adresáře:
   ```bash
   git clone <repository-url>
   cd <repository-directory>
   ```

2. Vytvořte a aktivujte virtuální prostředí (Virtual Environment):
   - **Na Linuxu / Raspberry Pi:**
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     ```
   - **Na Windows (Command Prompt):**
     ```cmd
     .venv\Scripts\activate.bat
     ```

3. Nainstalujte požadované závislosti:
   ```bash
   pip install -r requirements.txt
   ```

### Spuštění aplikace

1. Ujistěte se, že je virtuální prostředí aktivní a Arduino je řádně připojeno.
2. Spusťte backendový server:
   ```bash
   python app.py
   ```
3. Aplikace automaticky inicializuje SQLite databázi (`logs.db`), nahraje empirické hodnoty Lookup Table (LUT) pro přesný přepočet objemu na čas běhu čerpadla a spustí dedikované vlákno pro asynchronní sériovou komunikaci s Arduinem.

### Používání systému

Po úspěšném spuštění serveru jsou na lokální síti dostupné následující služby:

- **Frontend HMI:** [http://localhost:5000/](http://localhost:5000/) - Webový dashboard určený pro operátora (ovládání cílové dávky, monitoring IR senzorů, spuštění sekvence či proplachu, nastavení RGB podsvícení).
- **API Dokumentace:** [http://localhost:5000/apidocs/](http://localhost:5000/apidocs/) - Interaktivní Swagger rozhraní (Flasgger) dokumentující dostupné REST API endpoints pro telemetrii, diagnostiku a řízení.

## Lokální databáze (SQLite)

Systém pro svůj autonomní běh nevyžaduje externí databázový server. Soubor `logs.db` je generován automaticky při prvním spuštění aplikace a obsahuje následující strukturu tabulek:
- `logs`: Kontinuální záznam událostí (vydané sklenice, aktivace nouzového tlačítka E-STOP).
- `machine_stats`: Globální telemetrická data (celkový provozní čas, celkový přečerpaný objem v ml, jas LED).
- `servo_calibration`: Kinematická tabulka ukládající přesné pracovní úhly servomotoru pro jednotlivé pozice výdejních zásobníků.
- `volume_calibration`: Empirická kalibrační tabulka (LUT) pro lineární interpolaci doby běhu čerpadla v závislosti na požadované dávce.
