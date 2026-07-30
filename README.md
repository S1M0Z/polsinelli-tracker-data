# Polsinelli Tracker Data

Données, scanner de publications et moteur de décision conservateur pour le suivi des positions publiées par Laurent Polsinelli.

## Fichiers de données

- `positions.json` : positions ouvertes et clôturées
- `updates.json` : articles nouveaux, modifiés ou remis en avant
- `article-state.json` : état de déduplication
- `scan-log.json` : journal des scans Zonebourse
- `quote-history.json` : historique append-only des cotations récupérées
- `market-data-config.json` : fournisseur et correspondances des instruments
- `risk-policy.json` : seuils de fraîcheur, spread, dérive de prix et risque
- `investment-view.json` : vue calculée pour la prise de décision et le dashboard

## Cotations structurées par API

Le scraping de pages web n'est plus utilisé pour produire les cours. `scripts/quote_collector.py` interroge un fournisseur structuré, normalise chaque snapshot puis met à jour :

1. `quote-history.json` avec le prix, le bid, l'ask, les tailles, le délai et les horodatages ;
2. `positions.json` uniquement lorsqu'une cotation plus récente est disponible ;
3. `market-data-config.json` lorsqu'un mnémonique est résolu vers un identifiant fournisseur ;
4. `investment-view.json` après passage du moteur de décision.

Le premier fournisseur implémenté est **Saxo OpenAPI Info Prices**. La recherche part du mnémonique ou de l'ISIN. En cas d'ambiguïté, renseigne manuellement `uic` et `assetType` dans `market-data-config.json`.

### Token temporaire

```bash
cp .env.example .env
export SAXO_ACCESS_TOKEN="..."
export SAXO_ENV="sim"  # ou live
python scripts/quote_collector.py --session manual
python scripts/investment_engine.py
python scripts/validate_data.py
```

### Connexion OAuth Saxo locale

L'App Key et l'App Secret ne sont pas des jetons de marché à eux seuls. Une connexion Saxo initiale doit produire un access token et un refresh token. Le helper local capture le callback sur `localhost`, échange le code et écrit le bundle dans `.runtime/saxo-token.json`, fichier ignoré par Git.

```bash
export SAXO_ENV=sim
export SAXO_APP_KEY="..."
export SAXO_APP_SECRET="..."
python scripts/saxo_oauth_login.py
python scripts/quote_collector_oauth.py --session manual
```

Lorsqu'il expire, `quote_collector_oauth.py` renouvelle automatiquement l'access token à partir du refresh token et remplace atomiquement le bundle local, conformément à la rotation imposée par Saxo. Aucun token ni secret n'est imprimé.

L'accès temps réel dépend des droits de marché associés au compte et au jeton Saxo. Le collecteur conserve `delayedByMinutes` et abaisse la confiance lorsque la donnée est différée. Il n'invente jamais un prix à partir du seul sous-jacent.

### GitHub Actions

`.github/workflows/collect-market-quotes.yml` interroge l'API toutes les cinq minutes pendant la plage européenne, recalcule la vue et pousse un commit uniquement si les données changent.

Configurer dans **Settings → Secrets and variables → Actions** :

- secret `SAXO_ACCESS_TOKEN`
- secret facultatif `SAXO_ACCOUNT_KEY`
- variable `SAXO_ENV` avec `sim` ou `live`

Un simple App Key/App Secret ne suffit pas sur un runner GitHub éphémère : le refresh token Saxo tourne à chaque renouvellement. Pour une exécution permanente, utiliser le collecteur OAuth sur un VPS, un PC allumé ou un runner auto-hébergé qui conserve `.runtime/saxo-token.json`. Le dépôt ne stocke jamais le token en clair.

## Moteur de décision

Le moteur refuse par défaut toute entrée lorsque la cotation est ancienne, que le bid/ask manque, que le spread est trop large, que la détection est trop lente, que le prix a trop dérivé ou que le ratio rendement/risque est insuffisant.

```bash
python scripts/investment_engine.py
python scripts/validate_data.py
python -m unittest discover -s tests -v
```

Les seuils par défaut utilisent un capital de simulation de 10 000 € et supposent que la totalité de la prime d'un produit à effet de levier peut être perdue. Modifie `risk-policy.json` pour adapter la simulation à ton capital et à ta tolérance au risque.

## Important

Les résultats historiques reposent sur les prix d'entrée et de sortie publiés. Ils n'intègrent le spread, les frais, le slippage et le délai d'exécution que lorsqu'ils sont réellement présents dans `quote-history.json`. Ce dépôt fournit une aide à la décision, pas un système d'ordres automatiques ni un conseil en investissement.
