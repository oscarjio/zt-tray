# Imorgon: koppla ZT-Tray på etiketterarens RS485 (5 steg)

ZT-Tray **lyssnar bara** – den sänder aldrig något på bussen (sändaren är låst tills du själv aktiverar den i Avancerat).

## 1. Koppla in (A/B/0V – eller 4-tråd)
- **2-tråd:** adapterns **A (D+, ibland "485+")** → bussens A/+, **B (D−, "485−")** → B/−, och **0V/GND** → bussens 0V/signaljord
  (på SW1A4080: stift 5 = 0V_A). Koppla parallellt, helst nära PLC:n. Bryt inte bussen.
- **Ingen egen terminering** (inget 120 Ω i adaptern/jumper av). Bussen ska redan ha 120 Ω i båda ändar.
- **4-tråd (SW1A4080, CN5A/CN5B):** stift 1/2 = +RX/−RX, 7/8 = +TX/−TX, 5 = 0V. En adapter ser bara ena riktningen.
  Har du två adaptrar: en på varje par och välj **2:a port (4-tråd)** i Modbus-fliken. Har du bara en: börja på paret
  där PLC:n svarar etiketteraren och byt par om du bara ser frågor eller bara svar.
- Tips: mät spänningslöst A–B: ≈60 Ω = rätt terminering, ≈120 Ω = en saknas, ≈40 Ω = en för mycket.

## 2. Drivrutin
- Sätt i USB-RS485-adaptern. Öppna **Enhetshanteraren → Portar (COM och LPT)**: det ska stå t.ex.
  *USB-SERIAL CH340 (COMx)*, *USB Serial Port (FTDI)* eller *CP210x*.
- Gult utropstecken / "Okänd enhet": installera tillverkarens drivrutin (CH340: wch.cn, FTDI: ftdichip.com,
  CP210x: silabs.com). Kräver admin – be IT om det behövs. Dra ur och sätt i igen efteråt.

## 3. Starta
- Starta **zt-tray.exe** (senaste: https://github.com/oscarjio/zt-tray/releases/latest/download/zt-tray.exe).
- Fliken **Start** väljer adaptern själv och hittar baud/paritet (lyssnar ca 10–30 s). Om programmet startades före
  kabeln väntar det och startar självt när adaptern dyker upp. Flera adaptrar → den frågar vilken.
- Står det att porten är **upptagen**: stäng Seriemonitor-fliken, PuTTY, annan ZT-Tray eller leverantörsverktyg, tryck START.

## 4. Vad du tittar på
- **Stor status OK / VARNING / FEL** med klartext och vilken adress/enhet.
- **Busshälsa** (% lyckade svar senaste minuten) och en **rad per adress**: missade (timeout), sena, krockar, CRC, orsak.
- "Adress 1, 5 felar **SAMTIDIGT**" = bussproblem (störning, terminering, kabel/skärm, matning, master/timing).
  Fel var för sig = den enskilda enheten. "**Misstänkt adresskrock på adress N**" = två enheter har samma adress.
- När maskinen stannar: Modbus-fliken → **Vad hände innan stoppet…** visar sekunderna före stoppet.
  **Spara logg…** direkt efter felet (kan spelas upp igen med Öppna logg…).
- Dras kabeln ur visas "Adaptern tappad – återansluter automatiskt"; sätt tillbaka den, inget mer behövs.

## 5. Inga bytes – eller bara skräp
- **0 bytes:** fel par (4-tråd) eller ingen trafik just nu (maskinen står still?), A/B ej anslutna, fel COM-port,
  trasig adapter. Kontrollera skruvplintarna, att maskinen kör, prova andra paret, kolla Enhetshanteraren.
- **Bytes men bara skräp / CRC-fel på allt:** **byt A och B** (vanligast), kör sedan START igen. Saknas 0V: koppla 0V.
  Fortfarande skräp: Modbus-fliken → **Auto-sök**, eller ställ in baud/paritet från maskinens DIP/inställning
  (SW1A4080: se MODBUS-FELSOKNING.md). Skräp bara ibland och mest på långt bort-enheter → terminering/skärm.
- Varningen **"A och B omkastade"** → byt D+/D− och starta om.
