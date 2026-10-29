# CHECKLISTA – Modbus RTU-felsökning, etikettmaskin
Skriv ut. Bocka av. Detaljer: MODBUS-FELSOKNING.md.

## Utrustning
- [ ] Laptop + laddare
- [ ] **Isolerad** USB-RS485-adapter (FTDI-chip helst)
- [ ] Kabel, klämmor, kopplingsplint, liten skruvmejsel
- [ ] Extra ledare för **0V/GND**
- [ ] Multimeter
- [ ] 120 Ω-motstånd (2 st) för terminering vid behov
- [ ] Mobil för foton (etiketter, DIP, JMP700, 7-seg)

## Inställningar före start
- [ ] Adaptern syns som COM-port i Enhetshanteraren (notera COM-nr)
- [ ] Enhetshanteraren → COM-porten → Port Settings → Advanced → **Latency Timer = 1 ms**
- [ ] Adapterns egen terminering/bias **AV** (bussen är redan terminerad)
- [ ] zt-tray startad som admin → Modbus-fliken
- [ ] Monitorn **sänder aldrig**, den lyssnar bara

## Steg för steg
1. [ ] Koppla **parallellt** på bussen: A–A, B–B, **0V–0V**. Bryt inget.
2. [ ] Välj COM → **Auto-sök** (baud/paritet/stopp). Kontrollera CRC-fel ≈ 0.
   Ser allt ut som skräp? Byt A/B.
3. [ ] Namnge slavarna (vilken adress = gamla resp. nya SW1A4080).
4. [ ] Sätt **felfälla** på nya SW1A4080 (watch-slav).
5. [ ] **Logga till fil** (.zmlog) under hela körningen. Exportera CSV efteråt.
6. [ ] Registervy: jämför **gammal mot ny** (A/B) och notera avvikande register.
7. [ ] Mät spänning **0V nya ↔ 0V PLC/gamla** (DC och AC). > ~1 V är misstänkt.
8. [ ] Mät A–B i vila (ca 0,2–5 V) och resistans A–B med spänningen av (ca 60 Ω = rätt terminerad).
9. [ ] Jämför gammal/ny: **JMP700**, **DIP-switchar**, **firmware-etikett**. Fota.
10. [ ] Notera **7-segment-felkoden** på nya, och när den visas.
11. [ ] Spara logg, CSV och foton i en mapp med datum.

## Felbilder → trolig orsak
| Ser du | Trolig orsak |
|---|---|
| "INGET SVAR (timeout)" bara för nya, andra svarar | Fel adress/baud/paritet på nya (DIP, JMP700) eller fel firmware |
| Nya svarar ibland, ofta timeout | Svarstid nära PLC:ns timeout, störning, 0V saknas/jordskillnad |
| CRC-fel/skräp på allt | Fel baud/paritet i monitorn, A/B förväxlade, dålig terminering |
| CRC-fel bara när nya svarar | Nya sänder dåligt: jordpotential, saknad 0V, trasig driver |
| Exception-svar (fc+0x80) från nya | Nya stöder inte registret/funktionen. Jämför registervy och firmware |
| Gammal och ny svarar olika värden på samma register | Annan firmware eller annan konfig/skalning |
| Allt OK i monitorn men maskinen larmar | Logiken i PLC:n, timing eller fel slav pekas ut. Kolla 7-seg-koden |
| Störningar i takt med motor/frekvensomriktare | EMC: skärm, jord, kabeldragning |


---
## Uppdatering 2.3.0 – inför felsökningen av motordrivarna

**Läget:** felet kan sitta i någon av de 3 motordrivarna. Adresserna är kända men just nu syns ingen kommunikation.

### Före avfärd
- [ ] Ladda ner senaste exe: https://github.com/oscarjio/zt-tray/releases/latest/download/zt-tray.exe
- [ ] Installera drivrutin för USB-RS485-adaptern (FTDI VCP: https://ftdichip.com/drivers/vcp-drivers/, annars CH340/CP210x).
      Kontrollera att adaptern syns som COM-port i Enhetshanteraren – just nu finns INGEN COM-port på datorn.
- [ ] Ta med **två** USB-RS485-adaptrar om bussen är 4-tråd (se nedan), plus skruvplintar och korta kablar.
- [ ] Testa vyn hemma: fliken Modbus → **Simulera** (40 s med inbyggda fel, inkl. att adress 5 tystnar).

### Ställ in på plats
- [ ] Fliken Modbus → **Kända enheter**: skriv de 3 motordrivarnas adresser, t.ex. `1=Motor 1, 2=Motor 2, 3=Motor 3`
      (sparas). Varje enhet får en statusrad: **grön** = svarar, **gul** = sent/missar ibland,
      **röd** = PLC frågar men ingen svarar, **grå** = PLC frågar aldrig. Okända adresser som syns listas också.
- [ ] Porten väljs automatiskt (RS485-adapter). Annars välj i listan / tryck ⟳ efter inkoppling.

### Läs länkindikatorn först (översta raden)
| Indikator | Betyder | Gör |
|---|---|---|
| Grå: **Inga bytes** | Inget kommer in alls | Kabel/plint, rätt par, adapter/COM-port, är PLC:n igång och pollar? |
| Röd: **Bytes men inga giltiga ramar** | Signal finns men går ej att avkoda | **Byt A/B**, kör **Auto-sök** (baud/paritet/stopp) |
| Grön: **Giltiga Modbus-ramar** | Länken OK – titta på enhetsraderna | Röd motor = frågas men svarar ej → kolla den drivaren |

### 4-trådsbuss (SW1A4080)
- SW1A4080 är **4-tråd**: separata par för RX och TX. En adapter på ett par ser bara **ena riktningen**
  (bara frågor *eller* bara svar) → det ser ut som att ingen svarar fast allt fungerar.
- Lösning: koppla en adapter på varje par och välj **2:a port (4-tråd)** – trafiken slås ihop i samma vy.
- Har du bara en adapter: lyssna på ett par i taget och notera vad som syns (bara FRÅGA-rader = masterns par).

### Under felsökningen
- [ ] Sammanfattningen visar per adress: frågor, svar, missade %, sena, krockar, max ms och **orsak i klartext**.
- [ ] Stannar maskinen: rött fält **STOPP** + fönstret **Vad hände innan stoppet** öppnas – visar de 30 sista paketen
      och vilken adress som felade **först**.
- [ ] Paketvyn: dubbelklicka en adress i sammanfattningen för filter, eller skriv t.ex. `addr==2 && status!=ok`.
      SENT SVAR (gul/orange) = drivaren svarar efter timeout → höj timeout eller kolla drivarens svarsfördröjning.
      KROCK (lila) = två sändare samtidigt → dubbla adresser eller felaktig terminering/bias.
- [ ] **Spara logg…** när felet uppträtt (sista 120 s) – kan spelas upp igen med **Öppna logg…**.
- [ ] Seriemonitor-fliken finns för rå trafik (text/hex). Sändning är AV – slå inte på den medan PLC:n är master.
