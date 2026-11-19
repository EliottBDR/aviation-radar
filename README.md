# aviation-radar

Génère un radar ATC animé (SVG pur, sans JavaScript) à partir de tes statistiques GitHub, pour ton README de profil.

## Installation (depuis le site GitHub, 4 minutes)

1. Dans ton dépôt de profil (`TON_PSEUDO/TON_PSEUDO`), crée le fichier `.github/workflows/update-radar.yml` avec le contenu fourni dans `workflow-example.yml`.
2. Settings → Actions → General → Workflow permissions → **Read and write permissions** → Save.
3. Onglet Actions → *Update aviation radar* → **Run workflow**.
4. Ajoute dans ton `README.md` :

```html
<img src="./aviation-stats.svg" alt="Radar ATC" width="100%">
```

Aucun token à créer : le `GITHUB_TOKEN` d'Actions suffit (données publiques).

## Variables (optionnelles)

`MAX_AIRCRAFT` (1 à 8, défaut 5), `ROTATION_SECONDS` (4 à 30, défaut 10).
