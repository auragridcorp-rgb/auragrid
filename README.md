# AurAgrid Data API

AurAgrid provides high-frequency geospatial, energy, and maritime telemetry endpoints secured via L402 micropayments.

## Authentication & Access (L402)
1. Send a standard GET request to any endpoint.
2. Unauthenticated requests will receive an `HTTP 402 Payment Required` response.
3. The response headers contain an L402 Macaroon and a BOLT11 Lightning Invoice.
4. Pay the invoice to receive the settlement preimage.
5. Re-send the request with the `Authorization: L402 <macaroon>:<preimage>` header to access the data payload.

## Available Endpoints

### Retail Telemetry
* `/api/v1/esg/carbon` - Satellite-verified ESG carbon offset telemetry.
* `/api/v1/energy/grid` - Real-time energy grid frequency and renewable mix.
* `/api/v1/sar/commodities` - Synthetic aperture radar (SAR) commodity volume estimates.
* `/api/v1/maritime/congestion` - Global shipping chokepoint vessel queues and delays.
* `/api/v1/weather/micro-climate` - High-frequency pressure and wind vector telemetry.
* `/api/v1/logistics/cross-border` - Manufacturing yard density and freight velocity.

### Wholesale Batch Feeds (Institutional Tier)
* `/api/v1/batch/hyperscaler-power` - Power load predictions and cooling requirements for AI data centers.
* `/api/v1/batch/lng-supply-chain` - Global Liquid Natural Gas (LNG) carrier fleet tracking and ETAs.
* `/api/v1/batch/crop-moisture` - Soil moisture and precipitation matrices across agricultural belts.
* `/api/v1/batch/sts-anomalies` - Open-ocean Ship-to-Ship (STS) cargo transfer tracking.
* `/api/v1/batch/maritime-weather` - Wind, wave, and swell routing optimizations for shipping fleets.
* `/api/v1/batch/curtailment-risk` - Renewable energy curtailment predictions for wind and solar farms.
