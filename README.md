# Zonebourse Derivatives Tracker Data

Données, scanner de publications et moteur de décision conservateur pour le suivi de toutes les recommandations de Turbos et Warrants publiées sur la page globale Zonebourse.

## Fichiers de données

- `positions.json` : positions ouvertes et clôturées
- `updates.json` : articles nouveaux, modifiés ou remis en avant
- `article-state.json` : état de déduplication
- `scan-log.json` : journal des scans Zonebourse
- `quote-history.json` : historique borné des cotations récupérées (500 par position)
- `market-data-config.json` : fournisseur et correspondances des instruments
- `risk-policy.json` : seuils de fraîcheur, spread, dérive de prix et risque
- `investment-view.json` : vue calculée pour la prise de décision et le dashboard
- `health.json` : santé et fraîcheur par composant, générées dans chaque snapshot publié
- `public-manifest.json` : liste versionnée des fichiers publiés ensemble
- `schemas/` : schémas JSON versionnés des positions et de la politique de risque

## Cotations structurées

Les fiches publiques Euronext identifient correctement les produits suivis, mais leurs cotations sont injectées en JavaScript. Le collecteur opérationnel utilise donc Chromium via Playwright pour charger la page comme un navigateur, attendre le DOM final et extraire le dernier cours ainsi que le bid/ask.

Il met à jour :

1. `quote-history.json` avec le prix, le bid, l'ask, les tailles et les horodatages ;
2. `positions.json` uniquement lorsqu'une cotation plus récente est disponible ;
3. `market-data-config.json` lorsqu'un symbole Euronext est résolu ;
4. `investment-view.json` après passage du moteur de décision.

Aucune clé n'est nécessaire.

### Installation locale ou serveur

```bash
python3 --version  # Python 3.12 minimum
python3 -m pip install -r requirements-browser.txt
python3 -m playwright install chromium

export MARKET_DATA_PROVIDER=euronext
export EURONEXT_DEFAULT_MIC=XMLI
python3 scripts/quote_collector_browser.py --session manual
python3 scripts/investment_engine.py
python3 scripts/validate_data.py
```

Sur un serveur Linux, utiliser lors de la première installation :

```bash
python3 -m playwright install --with-deps chromium
```

Le provider essaie successivement les places configurées dans `market-data-config.json`, avec `XMLI`, `XPAR` et `SEDX` par défaut. Une erreur sur un produit ne provoque jamais l'invention d'un prix.

### GitHub Actions

Le workflow `.github/workflows/collect-market-quotes.yml` est volontairement limité au lancement manuel pendant la validation du rendu Chromium. La collecte permanente sera installée sur le serveur, où Chromium reste présent entre les passages au lieu d'être retéléchargé par un runner GitHub éphémère.

Les pages publiques Euronext ne constituent pas un flux garanti par contrat. Pour un service avec SLA et droits formels de redistribution, il faudra remplacer ce provider par Euronext Web Services ou un autre flux licencié. Le provider Saxo et son support OAuth restent présents comme solution facultative, mais Saxo SIM ne référençait pas les produits suivis lors des tests.

## OAuth Saxo facultatif

Le script `scripts/saxo_oauth_login.py` effectue la connexion initiale sur `http://localhost:8765/callback`. `scripts/quote_collector_oauth.py` renouvelle ensuite les jetons tournants dans `.runtime/saxo-token.json`, fichier exclu de Git. Cette voie n'est pas utilisée par le collecteur Euronext actif.

## Moteur de décision

Le moteur refuse par défaut toute entrée lorsque la cotation est ancienne, que le bid/ask manque, que le spread est trop large, que la détection est trop lente, que le prix a trop dérivé ou que le ratio rendement/risque est insuffisant.

Les recommandations et le portefeuille sont deux vues distinctes. Une recommandation ne devient une position détenue que si `portfolioStatus` vaut `held` et que `executedAt`, `executedPrice`, `quantity` et `account` sont renseignés.

```bash
python3 scripts/investment_engine.py
python3 scripts/validate_data.py
python3 -m unittest discover -s tests -v
```

Une migration idempotente des anciens documents est disponible avec `python3 scripts/migrate_schema.py` et est exécutée automatiquement sur le runtime serveur. Le validateur refuse par défaut un snapshot `SYSTEM_STALE` ou `DATA_PIPELINE_UNAVAILABLE`. La CI utilise `--mode structural`; la publication serveur conserve le mode opérationnel bloquant.

Les seuils par défaut utilisent un capital de simulation de 10 000 € et supposent que la totalité de la prime d'un produit à effet de levier peut être perdue. Modifie `risk-policy.json` pour adapter la simulation à ton capital et à ta tolérance au risque.

## Synchronisation Zonebourse

Le collecteur serveur lit toutes les cinq minutes les recommandations en cours et les positions soldées avec Chromium, car Zonebourse bloque les requêtes HTTP directes des runners GitHub. Pour les cartes incomplètes, il ouvre progressivement les fiches article afin d'extraire l'ISIN, le prix d'entrée, l'objectif et le seuil d'invalidation explicitement publiés. L'ISIN permet ensuite au collecteur Euronext de résoudre le produit et d'actualiser son bid/ask. Il n'enregistre une clôture que lorsqu'un prix d'entrée et un prix de sortie explicites sont publiés, puis reconstruit `investment-view.json`. Une recommandation qui reste sans données confirmées conserve des valeurs `null` et une décision bloquée.

Une recommandation absente de la liste ouverte passe d'abord à `PENDING_RECONCILIATION`, puis à `UNKNOWN` après trois cycles. Seule une sortie explicite issue de l'historique la clôture. Les articles incomplets restent dans une file persistante et les extractions ambiguës sont placées en quarantaine.

## Important

Les résultats historiques reposent sur les prix d'entrée et de sortie publiés. Ils n'intègrent le spread, les frais, le slippage et le délai d'exécution que lorsqu'ils sont réellement présents dans `quote-history.json`. Ce dépôt fournit une aide à la décision, pas un système d'ordres automatiques ni un conseil en investissement.
