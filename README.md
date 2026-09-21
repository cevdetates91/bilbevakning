# Bilbevakning – molnversion (GitHub Actions + Pages)

Den här versionen körs **helt automatiskt i molnet**, en gång i timmen, utan att
din dator behöver vara på. Resultatet visas på en webbsida du kan öppna från
vilken enhet som helst (mobil också).

Kostnad: **0 kr.** GitHub Actions och GitHub Pages är gratis för publika repon.

## Viktigt att veta innan du börjar

Repot måste vara **publikt** för att GitHub Pages ska vara gratis. Det innebär
att sidan är teknisk publik för den som har länken (inte lösenordsskyddad,
inte sökbar/indexerad av Google om du inte länkar den någonstans). Det är bara
bilannonser du redan letar efter på Blocket – inget känsligt – men värt att
veta.

## Steg 1: Skapa ett GitHub-konto (om du inte redan har ett)

Gå till [github.com/signup](https://github.com/signup) och skapa ett gratis konto.

## Steg 2: Skapa ett nytt repo

1. Klicka på **+** uppe till höger → **New repository**.
2. Namn: t.ex. `bilbevakning` (valfritt).
3. Välj **Public**.
4. Klicka **Create repository**. Lämna resten som standard (ingen README behövs, vi laddar upp egna filer).

## Steg 3: Ladda upp filerna

I ditt nya (tomma) repo:

1. Klicka på länken **"uploading an existing file"** (eller "Add file" → "Upload files").
2. Dra in **alla** filer och mappar från den här leveransen:
   - `bilbevakning_cloud.py`
   - `config.json`
   - `docs/index.html` (hela `docs`-mappen)
   - `.github/workflows/bilbevakning.yml` (hela `.github`-mappen, inklusive undermappen `workflows`)

   **Tips:** Om du drar in en hel mapp (t.ex. `docs` eller `.github`) i webbläsaren
   brukar GitHub bevara mappstrukturen automatiskt. Om det krånglar, ladda upp
   filerna en och en och skriv in hela sökvägen i filnamnsfältet, t.ex.
   `.github/workflows/bilbevakning.yml`.
3. Scrolla ner, skriv ett commit-meddelande (t.ex. "Första uppladdning"), klicka **Commit changes**.

## Steg 4: Ge Actions skrivbehörighet

Workflowen behöver kunna committa tillbaka resultatet.

1. I repot, gå till **Settings** (kugghjulet) → **Actions** → **General**.
2. Scrolla till **Workflow permissions**.
3. Välj **Read and write permissions**.
4. Klicka **Save**.

## Steg 5: Aktivera GitHub Pages

1. Fortfarande i **Settings** → klicka **Pages** i vänstermenyn.
2. Under **Build and deployment** → **Source**, välj **Deploy from a branch**.
3. Under **Branch**, välj `main` och mappen `/docs`. Klicka **Save**.
4. GitHub visar efter ett tag en länk högst upp, typ
   `https://dittanvandarnamn.github.io/bilbevakning/` – **det är din portal-länk.**
   Spara/bokmärk den (fungerar på mobilen också).

## Steg 6: Testkör direkt (istället för att vänta en timme)

1. Gå till fliken **Actions** i repot.
2. Klicka på workflowen **"Bilbevakning"** i vänsterlistan.
3. Klicka knappen **Run workflow** (uppe till höger) → **Run workflow** igen för att bekräfta.
4. Vänta ~1–2 minuter, uppdatera sidan – du ska se en grön bock när den är klar.
5. Öppna din Pages-länk från steg 5 – resultatet ska synas där.

Därefter körs den **automatiskt varje timme**, för alltid, helt utan att du
behöver göra något. Kolla `docs/index.html`-länken när du vill se aktuella kap.

## Ändra sökkriterier senare

Öppna `config.json` direkt i GitHub (klicka på filen → pennikonen för att redigera),
ändra värden, spara (commit). Nästa schemalagda körning (inom en timme) använder
de nya kriterierna automatiskt.

## Justera hur ofta den körs

Öppna `.github/workflows/bilbevakning.yml`, ändra raden `cron: "0 * * * *"`.
Exempel: `"0 */2 * * *"` = varannan timme, `"0 8,20 * * *"` = kl 08 och 20 varje dag.
(GitHub Actions cron körs i UTC, dvs svensk vintertid +1h, sommartid +2h.)

## Felsökning

Om sidan inte uppdateras: gå till fliken **Actions**, klicka på senaste körningen
och läs loggen – där syns exakt vad som gick fel (t.ex. om Blocket blockerat
GitHubs IP-adresser, vilket kan hända ibland eftersom molnservrar ibland
blockeras hårdare av bot-skydd än vanliga hemuppkopplingar).
