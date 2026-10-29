# Modbus RTU / RS485 – felsökningsguide (zt-tray Modbus-monitor)

Monitorn **lyssnar bara** (sänder aldrig). Koppla en USB-RS485-adapter parallellt på bussen
(A/B + 0V/GND), helst nära PLC:n. Lägg INTE till eget termineringsmotstånd i adaptern.

## Snabbstart
* UI: fliken **Modbus** → välj port → *Auto-sök…* (baud/paritet/stopp, poäng per kombination) eller ställ manuellt → *Starta*.
* Skriv slavnamn, t.ex. `1=LD2, 4=SW1A4080 gammal, 5=SW1A4080 ny`.
* *Bevakad slav (felfälla)*: t.ex. `5` → sparar N s före/efter varje fel till `modbus-loggar\felfalla_slav5_*.zmlog`.
* *Jämför markerade sida vid sida*: svarstider/timeouts/CRC/exceptions för gamla vs nya SW1A4080.
* *Registervy…*: alla register/coils PLC:n läser/skriver per slav (värde dec/hex, ändringshistorik,
  antal läsningar/skrivningar, senaste fel). Läge *Jämför* A mot B: gul = olika värde, röd = exception/inget svar,
  blå = bara den ena frågas/svarar. *Exportera CSV…* sparar registerdump eller jämförelse.
* Paketlistan visar i klartext `Läs holding-reg adr 100 antal 4 → Svar (3.4 ms): …`
  eller `… → INGET SVAR (timeout, …)`.
* CLI:
  ```
  python modbus_monitor.py --list
  python modbus_monitor.py --port COM5 --baud 19200 --parity E
  python modbus_monitor.py --port COM5 --auto
  python modbus_monitor.py --port COM5 --baud 19200 --parity E --trap 5 --compare 4 5 --log buss.zmlog
  python modbus_monitor.py --replay buss.zmlog --only-errors --compare-regs 4 5 --regs-csv jmf.csv
  python modbus_monitor.py --simulate --compare 4 5 --compare-regs 4 5
  python modbus_monitor.py --port rfc2217://192.168.1.50:47922 --baud 19200 --parity E
  ```

## Orsak → vad monitorn visar
| Orsak | Vad du ser i monitorn | Åtgärd |
|---|---|---|
| **Saknad/fel terminering** (120 Ω i BÅDA ändar, inte fler) | CRC-fel och skräp som ökar med kabellängd/baud, oftast på avlägsna slavar; UART ram-/paritetsfel | Mät A–B spänningslöst: ≈60 Ω = rätt, ≈120 Ω = en saknas, ≈40 Ω = tre stycken |
| **Ingen bias (fail-safe)** | Skräp-byte i tystnaden mellan ramar, "kort paus"-varningar, CRC-fel på första byten | Bias-motstånd (t.ex. 680 Ω–1 kΩ) på ett ställe, oftast master |
| **Stubbar/stjärntopologi** | Fel beroende på vilken slav som svarar; långa grenar ger CRC-fel | Daisy-chain in/ut i varje enhet (SW1A4080 har CN5A/CN5B för detta) |
| **Jord/skärm/0V saknas** | Slumpmässiga CRC/skräp, mer när motorer går; ram-/paritetsfel | Dra 0V (SW1A4080: CN5 stift 5, 0V_A) mellan alla, skärm jordad i en ände |
| **Adresskrock** (två enheter samma ID) | **DUBBELSVAR**, CRC-fel (kollision), "svar från fel adress" | Kontrollera DIP ID6..ID0 – ny enhet levereras ofta med fabriksadress |
| **Fel baud/paritet på en slav** | Den slaven: bara TIMEOUT; UART-fel när den försöker svara; Auto-sök visar flera kombinationer med poäng | Jämför DIP BD2..BD0 och paritet i SL_Modbus |
| **Långsam svarstid / response delay** | Hög RTT (max/p99) för en slav i jämförelsen, timeouts nära PLC:ns gräns | Höj PLC-timeout, minska response delay i drivet |
| **PLC-timeout/retry för kort** | TIDIG ("master sände ny fråga … utan svar"), svar som kommer efter nästa fråga | Sätt monitorns *Timeout ms* = PLC:ns, öka i PLC |
| **För hög pollningstakt** | Kort paus (< 3,5 tecken) före frågor, slavar hinner inte | Lägg in paus mellan frågor, färre register per cykel |
| **EMI från stegmotordrivare** | Fel korrelerar med rörelse: CRC/skräp i skurar; felfällan visar mönster | Separera kablar, skärmad tvinnad parkabel, ferrit, filter på drivens matning |
| **Exception från slav** | EXCEPTION med kod (1 otillåten funktion, 2 otillåten adress, 3 värde, 4/6 upptagen) | Registervyn visar vilket register; jämför med gamla enheten |
| **Watchdog i drivet** | Slav tystnar efter timeout-skur, sedan OK igen | Kontrollera LED/statuskod, comm-watchdog-parameter |

## Ever Elettronica SW1A4080 – vad källorna säger
Källa: kort manual *SW1A4080* (https://www.evermotionsolutions.com/Uploads/Docs/short-sw1a4080-gb_1731.pdf),
produktlinje SW1 (https://www.evermotionsolutions.com/Uploads/Docs/sw1-drives-line-gb_1737.pdf),
fullständiga manualer: http://www.everelettronica.it/manhw.html
* Modbus-variant: SW1A4080N3B1-00 (option c0490 = Modbus slave); konfiguration med **SL_Modbus**.
* RS485 är **4-tråd-stil** på CN5A/CN5B: stift 1/2 = +RX/−RX, 7/8 = +TX/−TX, 5 = 0V_A.
  CN5A och CN5B är parallella (in/ut för daisy chain). På en 2-trådsbuss måste TX/RX byglas rätt (+TX↔+RX, −TX↔−RX).
* **JMP700**: läge 1 = 120 Ω på TX-linjen, läge 2 = 120 Ω på RX-linjen. Bara sista enheten på bussen ska ha dem!
* **DIP baud** BD2..BD0: OFF OFF OFF=115200, OFF OFF ON=57600, OFF ON OFF=38400, OFF ON ON=19200,
  ON OFF OFF=9600, ON OFF ON=4800, ON ON OFF=2400, ON ON ON=1200.
* **DIP nod-ID** ID6..ID0 binärt (0 ej tillåtet).
* LED-statusar inkluderar watchdog-ingrepp och *EEPROM write overrun / near end of life* (om PLC:n skriver
  parametrar cykliskt kan EEPROM slitas).
* Paritet, response delay och comm-watchdog finns inte i korta manualen – läs ut med SL_Modbus/fullständig manual.

## Checklista: gamla vs nya SW1A4080
1. Exakt artikelnummer/option (c0490 Modbus) och firmware/software-version på etiketten – samma?
2. DIP BD2..BD0 (baud) – samma på båda och som PLC:n?
3. DIP ID6..ID0 – unik adress, ingen krock (titta efter DUBBELSVAR)?
4. JMP700 – fabriksinställning på den nya kan ha 120 Ω PÅ. Bara ändenheten ska ha terminering.
5. Kablage CN5A/CN5B: TX/RX-bygling för 2-tråd, 0V_A ansluten, skärm.
6. SL_Modbus: exportera parametrar från båda och jämför (paritet, stoppbitar, response delay, watchdog).
7. Monitorn: *Jämför sida vid sida* (RTT min/medel/max/p99, timeouts, CRC, exceptions).
8. Registervyn i jämförelseläge: register som bara gamla svarar på, exceptions på nya (t.ex. kod 2 otillåten
   dataadress = annan firmware/registerkarta), olika parametervärden.
9. Byt plats på de två driven (eller adresser) – följer felet enheten eller platsen/kabeln?

## Rekommenderad hårdvara
* Isolerad USB-RS485 med **FTDI**-chip (t.ex. Waveshare USB TO RS485 (B) isolerad, Moxa UPort 1150I).
  Sätt **Latency Timer = 1 ms** (Enhetshanteraren → port → Avancerat), annars blir svarstider ±16 ms osäkra.
* Ingen terminering i adaptern, kort stubb, anslut GND/0V.
* 4-tråds-SW1A4080: lyssna på det par där både frågor och svar finns (på 2-trådsbuss är de ihopbyglade).

## Begränsningar
* USB-adaptrar levererar data i block → tider ±latency (1 ms med FTDI inställd). Kort paus-varning bara när mätbar.
* 1 vs 2 stoppbitar går ofta inte att skilja passivt; spec: utan paritet → 2 stopp, med paritet → 1.
* RFC2217-delning (serialshare) tar en klient åt gången.
* Registervyn ser bara register som PLC:n faktiskt frågar efter; värden från FC01/02 är bitar, FC03/04 16-bit.

## Automatisk diagnos (2.4.0)

| Varning i Auto-sök | Betyder | Åtgärd |
|---|---|---|
| Baud för högt | Bytes splittras, många ramfel, uppskattad baud < inställd | Sänk till uppskattat värde |
| Baud för lågt | Få långa ”bytes”, 0x00/0xFF-skräp, break | Höj baud |
| Fel paritet | Paritetsfel i nästan varje byte, CRC-ramar bara utan paritet | Byt E/O/N |
| Fel stoppbitar | Ramfel vid 2 stopp, ramar OK vid 1 (eller tvärtom) | Justera stoppbitar |
| A/B omkastade | Linjen vilar lågt: break och skräpskurar i takt med trafiken, inga giltiga ramar | Byt D+/D− |
| Slav X avviker | En slav har UART-fel eller trasiga svar, de andra är rena | Kontrollera den slavens inställning och kabel |

AI-fliken kan köra samma diagnos som ett verktyg, och kan (om du tillåter det) ändra
inställningar och skicka testfrågor. Se README.

## v2.5 – Startskärm, agentläge och Cursor-nyckel

**Nästan one-click:** plugga i USB-RS485-kabeln och starta programmet. Fliken **Start** väljer adaptern själv
(frågar bara om flera finns), startar lyssning, hittar baud/paritet/stoppbitar automatiskt (lyssnar bara, sänder inget)
och börjar bevaka.

Startskärmen visar:
- Stor status **OK / VARNING / FEL** och klartext om vad som är fel och vilken enhet.
- **Busshälsa** (% lyckade transaktioner senaste minut och totalt) med tidskurva: svar ok / missade / sena / krockar / CRC.
- En rad per adress: hälsa, missade, sena, krockar och senaste felorsak – felande adresser överst.
- Flera felande adresser analyseras gemensamt: fel **samtidigt** = troligen bussproblem, fel **var för sig** = enhetsproblem.
- Agentens meddelanden och paketen rullar löpande. Paketlista, register och sändare finns under **Avancerat ▸**.

**AI (valfritt):** Under Avancerat → AI-felsökning väljs leverantör *OpenRouter* eller *Cursor*. Cursor använder
Cursors officiella SDK lokalt (debiteras mot din Cursor-plan). SDK-komponenten (~48 MB, proprietär licens) laddas ner
från PyPI första gången och kontrolleras med sha256. Nyckeln sparas i config (visas maskerad) eller i miljövariabeln
`CURSOR_API_KEY` – aldrig i loggar.

**Autoläge:** med nyckel analyserar agenten bussen var 60:e sekund och direkt vid allvarligt fel (timeout-skur, krock,
hälsa under 90 %). Det stängs av efter 10 min (`ai.auto_minutes` i config) med tydligt meddelande – tryck
**Kör 10 min till**. Nedräkning och tokenanvändning visas. Manuella frågor fungerar alltid. Utan nyckel fungerar
autodetektering och lokal status ändå. AI får bara sända efter att du aktivt tillåtit det och bekräftat.

**Adresskrock (v2.5.1):** två enheter på samma adress upptäcks även om bara en adress krånglar - dubbla svar, CRC/krockar koncentrerade till adressen, varierande längd/registervärden för samma fråga, två kluster i svarstid, ibland exception, förvrängda svar med rätt adress. Visas med konfidens som 'Misstänkt adresskrock på adress N' på startskärmen, adressraden, i konsolen och som larm. AI-agenten har verktyget address_clash.
