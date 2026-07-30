# Polsinelli Tracker Data

Données et moteur de décision conservateur pour le suivi des positions publiées par Laurent Polsinelli.

## Fichiers de données

- `positions.json` : positions ouvertes et clôturées
- `updates.json` : articles nouveaux, modifiés ou remis en avant
- `article-state.json` : état de déduplication
- `scan-log.json` : journal des scans Zonebourse
- `quote-history.json` : historique append-only des cotations récupérées
- `risk-policy.json` : seuils de fraîcheur, spread, dérive de prix et risque
- `investment-view.json` : vue calculée pour la prise de décision et le dashboard

## Moteur de décision

Le moteur refuse par défaut toute entrée lorsque la cotation est ancienne, que le bid/ask manque, que le spread est trop large, que la détection est trop lente, que le prix a trop dérivé ou que le ratio rendement/risque est insuffisant.

```bash
python scripts/investment_engine.py
python scripts/validate_data.py
python -m unittest discover -s tests -v
```

Les seuils par défaut utilisent un capital de simulation de 10 000 € et supposent que la totalité de la prime d'un produit à effet de levier peut être perdue. Modifie `risk-policy.json` pour adapter la simulation à ton capital et à ta tolérance au risque.

## Important

Les résultats historiques reposent sur les prix d'entrée et de sortie publiés. Ils n'intègrent pas automatiquement le spread, les frais, le slippage ni le délai d'exécution tant que ces données ne sont pas présentes dans `quote-history.json`. Ce dépôt fournit une aide à la décision, pas un système d'ordres automatiques ni un conseil en investissement.
