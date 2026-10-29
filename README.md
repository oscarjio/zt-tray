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
`test_modbus_monitor.py` (17 tester). Se **MODBUS-FELSOKNING.md** för guide, SW1A4080-fakta och CLI-exempel.

