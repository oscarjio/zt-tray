# ZT-Tray v2

Version 2.1.1.

Snabbt IP-byte på nätverkskortet, hitta PLC:ns IP på kabeln och ZeroTier-platser – allt från aktivitetsfältet.

## Ikonen

| Färg | Betyder |
|---|---|
| Grön | Statisk IP |
| Blå | DHCP med adress |
| Orange | DHCP men ingen server svarar (169.254.x.x) – typiskt med kabel direkt mot PLC |
| Grå med streck | Ingen kabel / länk nere |
| Lila märke med Z | ZeroTier-plats ansluten (VPN) |
| Tre punkter | IP-byte pågår |

Håll muspekaren över ikonen för att se kort, läge, IP och VPN-status i klartext.

## Användning

- **Högerklick** – menyn: välj nätverkskort, klicka en profil för att byta direkt, *DHCP (automatisk)* för att gå tillbaka, *Manuell IP…*, *Hitta PLC…*, ZeroTier.
- **Vänsterklick** – öppnar fönstret (Snabbyte, Hitta PLC, IP-profiler, ZeroTier, Inställningar).
- Profiler med `?` (t.ex. `10.20.?.200`) frågar efter det som saknas. Senaste värdet är förifyllt – skriv och tryck Enter.

Bytet görs med `netsh` direkt på kortet utan gateway (så internet via WiFi påverkas inte), vanligtvis på under en sekund.

## Hitta PLC (kräver Npcap)

Menyvalet är grått tills [Npcap](https://npcap.com/#download) är installerat. Starta om ZT-Tray efter installationen.

*Sök kabel* lyssnar på kortet, pingar broadcast och ARP-sveper profilernas nät tills första svar.
*Sök nätet* sveper hela det nät kortet redan har (plus profilerna) och listar alla som svarar.
Siemens, FX5 (Mitsubishis MAC-prefix) och Beijer-skärmar märks i kolumnen Enhet.
Datorns egen IP spelar ingen roll vid kabelsök. Markera enheten och tryck *Sätt min IP i samma nät* (eller dubbelklicka).

## Uttag (dela kabeln till en annan dator)

Båda datorerna kör ZT-Tray och är anslutna till samma ZeroTier-nät.

- **Uttag hos kund** är datorn där kabeln sitter i PLC:n. Den får ingen IP. Adressen den hade läggs tillbaka när du stoppar.
- **Verktyg** är datorn med TIA Portal, GX Works eller SoMove (kollega eller RDP). Välj ett annat kort än det som har internet, sätt en IP i PLC:ns nät där, och låt programmet använda det kortet.

Npcap måste vara installerat på båda för nätläget. ZeroTier-kortet är bara transporten och går inte att välja som uttag.

Medan en delning är startad byts ikonen i aktivitetsfältet. Orange med paus betyder att den är igångsatt men väntar. Grön med bock betyder att länken är uppe. Symbolen visar nät, USB eller COM.

Den som delar ett nätverksuttag sätter en PIN med 4–8 siffror. Datornamnet syns då i listan hos alla andra ZT-Tray på samma ZeroTier-nät. Den som vill koppla på svarar ja och skriver samma PIN. Utan rätt PIN går inga ramar. Fem fel från samma adress pausar nya försök i en minut. PIN-koden skickas inte ut på nätet och ligger kvar i `config.json` på datorn som delar.

**USB** delar en enhet till verktygssidan. Uttagsdatorn behöver [usbipd-win](https://github.com/dorssel/usbipd-win) (`winget install --exact dorssel.usbipd-win`). Verktygsdatorn behöver [usbip-win2](https://github.com/vadimgrn/usbip-win2/releases), som använder en testsignerad drivrutin. ZT-Tray slår inte på testsignering själv.

**COM** delar en serieport med RFC 2217. Verktygsdatorn behöver [com0com](https://sourceforge.net/projects/com0com/) så att TIA får en lokal COMx. Baud, paritet och stoppbitar ställs där och skickas till uttagsdatorn. En USB-serieadapter kan i stället delas som USB.

## Inställningar

- **Starta automatiskt vid inloggning** – skapar en uppgift i Schemaläggaren (med admin, startar även på batteri, stängs aldrig av efter 72 h).
- **Fäst ikonen i aktivitetsfältet** – Windows 11 22H2 och senare. Första gången kan det ta några sekunder innan den flyttar ut från ^-pilen.

## Konfig

`config.json` bredvid exe:n, samma format som v1 – `sites` är oförändrat och en gammal fil fungerar direkt.
Nya nycklar läggs till automatiskt:

```json
"ip_presets": [
  { "name": "10.20.?.x", "network": "10.20.?", "host": 200, "prefix": 24, "gateway": "" }
],
"settings":   { "interface": "Ethernet", "notifications": true, "pin_tray": true },
"discovery":  { "duration": 5, "extra_subnets": ["192.168.2", "192.168.250"] }
```

## Bygga

Kör `build.bat` på en Windows-dator med Python 3.10+. Resultatet hamnar i `dist\zt-tray.exe`.
För att köra utan att bygga: `pip install pystray Pillow sv-ttk scapy` och sedan `python zt_tray.py` (begär admin själv).

Loggen hamnar i `zt-tray.log` bredvid exe:n.

## Modbus RTU-monitor (fliken Modbus)
Passiv bussmonitor för Modbus RTU/RS485 – sänder aldrig. Avkodar FC 01–06, 0F, 10, 17, 2B och exceptions,
parar fråga→svar med svarstid (`Fråga → Svar (x ms)` / `Fråga → INGET SVAR (timeout)`), flaggar timeout,
CRC-fel, exception, fel adress, dubbelsvar, skräp, kort paus och tidig master. Statistik per slav
(min/medel/max/p99), jämförelse sida vid sida, **registervy** (alla register per slav med värde, historik,
läs/skriv, fel; jämförelse mellan två slavar; CSV-export), rålogg (.zmlog) + uppspelning, CSV, felfälla och auto-sök
av baud/paritet/stopp. Filer: `modbus_monitor.py` (motor + CLI), `modbus_tab.py` (UI),
`packet_view.py`, `serial_tab.py`, `test_modbus_monitor.py` + `test_modbus_extra.py` (26 tester). Se **MODBUS-FELSOKNING.md** för guide, SW1A4080-fakta och CLI-exempel.

### Nytt i 2.3.0
- **Portväljare**: COM-portar med beskrivning/tillverkare/VID:PID/serienr, ⟳ och hotplug (var 2 s). RS485-adapter
  (FTDI USB-RS485, Waveshare, Moxa UPort 1150 …) väljs automatiskt, annars första USB-serial; senaste port sparas.
- **Länkindikator**: Inga bytes / Bytes men inga giltiga ramar (byt A/B, kolla baud/paritet) / Giltiga Modbus-ramar,
  med byte/s och andel ramar med korrekt CRC.
- **Kända enheter** (adress=namn, sparas): grön = svarar, gul = sent/missar ibland, röd = frågas men svarar ej,
  grå = frågas aldrig. Okända adresser som dyker upp visas också.
- **Sammanfattning per adress**: frågor, svar, missade (%), sena, krockar, max ms, orsak i klartext
  (timeout / CRC-fel / exception X / svar från annan adress / krock). Missade svar per adress syns alltid.
- **Vad hände innan stoppet**: vid > 2 s tystnad efter fel eller när PLC:n slutar fråga en adress – senaste 30
  paketen och vilken adress som felade först.
- **Halv duplex**: SENT SVAR (efter timeout eller efter nästa fråga) och KROCK (överlappande/trasiga ramar).
  Timeout ställs i UI; svarstid färgas grön/gul nära gränsen/röd över.
- **Wireshark-lik paketvy**: nr/tid/delta/frågad/svarande/fc/info/svarstid/status, färgregler, filter
  (`addr==5`, `fc==3`, `status!=ok`, `timeout`, `sent`, `krock`, `reg==100`, `ms>50`, `&&`, `||`, `!`), fritextsök
  (hex/text) med nästa/föregående, detaljpanel med fält + hexdump, fråga↔svar markeras. Spara/öppna logg (.zmlog).
- **4-tråd**: välj en 2:a port – trafiken från två adaptrar (RX- och TX-paret) slås ihop i samma vy.
- **Simulera**-knapp: 40 s syntetisk buss med inbyggda fel – för att lära sig vyn utan hårdvara.
- **Seriemonitor-flik**: generell konsol (port/baud/paritet/stopp, text/hex/båda, tidsstämplar, ny rad vid tystnad
  eller tecken, logg till fil). Sändning är AV som standard och tydligt varningsmärkt.
- GitHub Actions bygger exe och bifogar den till release vid tagg `v*`.
  Senaste exe: https://github.com/oscarjio/zt-tray/releases/latest/download/zt-tray.exe

### Nytt i 2.4.0 – Auto-sök, Sändare och AI-felsökning

**Auto-sök (fliken Modbus)** – koppla på adaptern och tryck *Auto-sök*. Programmet lyssnar
på flera hastigheter och paritet/stoppbit-kombinationer, och poängsätter dem efter giltiga
CRC-ramar. Det visar en *säkerhet i %* och varnar för:
- **Baud för högt/lågt** (uppskattar den verkliga hastigheten från bitlängden i felbytes)
- **Fel paritet / stoppbitar** (ramfel kontra paritetsfel)
- **A/B omkastade** (linjen vilar lågt: break-tillstånd och skräpskurar utan giltiga ramar)
- **Enskilda slavar som avviker** (t.ex. en slav med fel paritet eller fel baud)

*Använd bästa* ställer in monitorn på det som vann. Kolumnen *UART-fel* i slavtabellen visar
ramfel och paritetsfel per slav.

**Sändare (Modbus Poll-liknande master)** – FC 1, 2, 3, 4, 5, 6, 15 och 16, pollning,
adresskanning och värdeformat (u16/s16/hex/u32/s32/f32). Sändaren är **AV som standard**
och måste aktiveras **en gång per programstart**: *Sändning på buss i drift kan störa*.
Om lyssnaren är igång delas porten (TX syns i monitorn). Annars öppnar sändaren en egen port.

**AI-felsökning** – en chatt som har verktyg mot den fångade trafiken: status, slavstatistik,
paketsökning, paket runt en tidpunkt, felhändelser, incidenter, registervärden, auto-sök,
byte av baud/paritet/stoppbit, start/stopp och felfällor.
- API: OpenAI-kompatibelt. Standard är OpenRouter med en billig DeepSeek-modell, och modellen
  går att välja. Klistra in nyckeln och tryck *Spara*. Den sparas i den lokala `config.json`
  (som är gitignorerad), visas maskerad och skrivs aldrig till loggar.
  Alternativt kan du sätta miljövariabeln `OPENROUTER_API_KEY`.
- **Automatisk övervakning**: kryssa i, eller skriv t.ex. *"Vi har ett fel på modbus, det
  kommer 3 ggr per halvtimme, sätt igång automatisk övervakning."* Programmet för då en
  långtidsjournal med alla felhändelser, minutstatistik och incidentkluster, med en
  sammanfattning var 10:e minut. Journalen sparas också som `modbus-loggar/ai-journal_*.jsonl`.
- När det strular skriver du **"nu strular det"**. Tidpunkten markeras och AI:n söker själv
  igenom trafiken runt markeringen och incidenterna.
- **Sändning från AI** kräver att du kryssar i *Tillåt AI att sända* och att Sändaren är
  aktiverad. Varje sändning bekräftas dessutom i en dialogruta.
- Förbrukningen av tokens och kostnaden visas under chatten. En typisk analys kostar
  ungefär $0.002–0.003.
- Cursor SDK utvärderades. Den kör kodagenter (beta och kräver en Cursor-nyckel), inte
  chatt mot trafikdata, så programmet använder i stället ett vanligt OpenAI-kompatibelt API.

**Begränsningar:** USB-RS485-adaptrar ger inte exakt bittiming. Baud-uppskattningen och
A/B-detekteringen är därför heuristiska och ska ses som indikationer med säkerhetsvärde.
