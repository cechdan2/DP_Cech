#  Diplomová práce

## O projektu

Tento repozitář obsahuje zdrojové kódy k diplomové práci zabývající se návrhem a realizací mechatronického automatizovaného dávkovače s pohyblivým ramenem. Celý systém je vestavěn do fyzického modelu sklízecí mlátičky Bruder John Deere T670i.

Architektura projektu je rozdělena do dvou hlavních vrstev:
- **Nízkoúrovňové řízení (Arduino UNO):** C++ firmware obsluhující hardwarové periferie – IR senzory (38kHz), PWM řízení kapalinového čerpadla, polohování servomotoru a adresovatelné RGB LED podsvícení. Stará se o stavový automat dávkovací sekvence a nouzové stavy (E-STOP).
- **Vysokoúrovňové řízení a HMI (Raspberry Pi):** Backend postavený na Pythonu a mikroframeworku Flask. Zajišťuje sériovou komunikaci s Arduinem (UART s vlastním XOR checksum protokolem), ukládání provozních dat a kalibračních tabulek do SQLite databáze a poskytuje frontendové webové rozhraní.

## Struktura projektu

```text
/dp_cech
 ├── app.py
 ├── ARDUINO.txt
 ├── requirements.txt
 ├── README.md
 ├── LICENSE
 └── templates/
      └── index.html
