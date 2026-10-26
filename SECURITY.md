# Security

Only public Binance Spot market data is used; no exchange keys, accounts or trading.
Bind the backend to loopback and expose it through an HTTPS reverse proxy.
FireCharts is public, without invitation links or owner accounts.

Never commit production data/, backups, raw feeds, logs, cookies, private deployment
configuration or environment files. Keep LICENSE/NOTICE files for dependencies.
Review HTTP route/static allowlists and run the tests before deploying.

When reporting vulnerabilities, omit secrets and private server data from public issues.
