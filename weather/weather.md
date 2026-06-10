
Moon Dev Open Source

This Bot Finds Mispriced Weather Markets on Polymarket Before the Crowd Catches On
Polymarket lists temperature prediction markets for dozens of cities around the world. The crowd bets on what tomorrow's high will be. But weather forecasts are free, public data. This scanner pulls real forecasts and compares them to the market consensus — then flags every market where the weather models disagree with the money.

By Moon Dev · March 27, 2026

Why Weather Markets Are Algorithmic Trading Gold
Here's the thing about prediction markets: they're only as smart as the people betting on them. And when it comes to weather, most people are guessing. They look out the window, check a vague weather app, and throw a few bucks at a number. Meanwhile, professional weather models running on supercomputers are producing hourly forecasts for every city on earth — and that data is completely free.

That gap — between what the crowd thinks the temperature will be and what the weather models say it will be — is your edge. This scanner automates finding that gap across every single temperature market on Polymarket, for every city, every day.

What Makes This Scanner Different
This isn't just scraping market prices. The scanner fetches real meteorological data from Open-Meteo (a free weather API — no API key required) including both standard forecasts and ensemble forecasts from 51 independent weather models. It then compares the forecast high temperature to the market's favorite bucket and calculates an "edge score" in degrees. When the forecast and the market disagree by 2+ degrees, you've got a potential trade.

The bot scrapes Polymarket for all tomorrow's temperature slugs, fetches market data from the Gamma API, pulls weather forecasts from Open-Meteo, ranks everything by 24-hour volume, and displays a detailed breakdown showing exactly where the data disagrees with the crowd. Let me walk you through how it works, piece by piece.

Join tomorrow's live Zoom call here

Step 1: Imports and Configuration
The scanner is a single Python file with zero paid API dependencies. That's intentional — you shouldn't need to pay for anything to find mispriced weather markets. The requests library handles all HTTP calls, termcolor makes the terminal output pretty, and the rest is standard Python.

The configuration block controls how often the scanner refreshes (every 30 minutes by default), how many cities get the full detailed breakdown (top 10 by volume), and the weather cache settings. Caching is important here — you don't want to hammer the Open-Meteo API 70+ times every scan cycle when the forecast only changes once an hour.

Imports and configuration
python
Click to copy
"""
Moon Dev's Weather Market Scanner v1.0
==========================================
Scans ALL Polymarket temperature markets for tomorrow, ranked by volume.
Shows real weather forecast data from Open-Meteo (FREE, no API key).
Highlights mispricing where forecast disagrees with market favorite.

Loops every 30 minutes. Ctrl+C to exit.

Built with love by Moon Dev
"""

import requests
import re
import time
import os
import json
import math
from datetime import datetime, timedelta
from termcolor import colored

# ============================================================================
# CONFIGURATION - Moon Dev's Weather Scanner
# ============================================================================

REFRESH_MINUTES = 30          # How often to refresh (minutes)
TOP_N_DETAILED = 10           # Show detailed view for top 10 cities by volume
COUNTDOWN_INTERVAL = 60       # Print countdown every N seconds
WEATHER_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".weather_cache.json")
WEATHER_CACHE_MAX_AGE = 3600  # Cache weather forecasts for 1 hour (seconds)
Notice the WEATHER_CACHE_MAX_AGE of 3600 seconds (1 hour). Weather forecasts don't change second by second — but market prices do. So we cache weather data aggressively while always fetching fresh market data. This keeps the scanner fast and respectful of the free API.

Step 2: City Coordinates and Display Helpers
To get a weather forecast, you need latitude and longitude. Rather than calling a geocoding API (which would add latency and another dependency), the scanner hardcodes coordinates for every city that Polymarket lists temperature markets for. This is a design choice: speed over flexibility. If Polymarket adds a new city, you just add one line to the dictionary.

The display helpers handle formatting — converting Celsius to Fahrenheit (because American traders exist), formatting dollar amounts with K/M suffixes, and converting URL slugs into pretty city names. Small details, but they make the terminal output readable at a glance.

City coordinates and formatting helpers
python
Click to copy
CITY_COORDS = {
    "atlanta": (33.749, -84.388),
    "seoul": (37.5665, 126.978),
    "shanghai": (31.2304, 121.4737),
    "wellington": (-41.2866, 174.7756),
    "london": (51.5074, -0.1278),
    "chicago": (41.8781, -87.6298),
    "nyc": (40.7128, -74.006),
    "tokyo": (35.6762, 139.6503),
    "buenos-aires": (-34.6037, -58.3816),
    "shenzhen": (22.5431, 114.0579),
    "singapore": (1.3521, 103.8198),
    "miami": (25.7617, -80.1918),
    "paris": (48.8566, 2.3522),
    "chongqing": (29.4316, 106.9123),
    "hong-kong": (22.3193, 114.1694),
    "ankara": (39.9334, 32.8597),
    "wuhan": (30.5928, 114.3055),
    "beijing": (39.9042, 116.4074),
    "warsaw": (52.2297, 21.0122),
    "seattle": (47.6062, -122.3321),
    "lucknow": (26.8467, 80.9462),
    "dallas": (32.7767, -96.797),
    "madrid": (40.4168, -3.7038),
    "chengdu": (30.5728, 104.0668),
    "sao-paulo": (-23.5505, -46.6333),
    "toronto": (43.6532, -79.3832),
    "munich": (48.1351, 11.582),
    "los-angeles": (34.0522, -118.2437),
    "tel-aviv": (32.0853, 34.7818),
    "milan": (45.4642, 9.19),
    "taipei": (25.033, 121.5654),
    "denver": (39.7392, -104.9903),
    "austin": (30.2672, -97.7431),
    "san-francisco": (37.7749, -122.4194),
    "houston": (29.7604, -95.3698),
}

CITY_DISPLAY = {
    "nyc": "New York City",
    "buenos-aires": "Buenos Aires",
    "hong-kong": "Hong Kong",
    "sao-paulo": "Sao Paulo",
    "tel-aviv": "Tel Aviv",
    "san-francisco": "San Francisco",
    "los-angeles": "Los Angeles",
}

def display_name(city_slug):
    """Convert slug to pretty display name."""
    if city_slug in CITY_DISPLAY:
        return CITY_DISPLAY[city_slug]
    return city_slug.replace("-", " ").title()

def fmt_usd(val):
    """Format USD amount."""
    if val >= 1_000_000:
        return f"${val/1_000_000:.1f}M"
    if val >= 1_000:
        return f"${val:,.0f}"
    return f"${val:.0f}"

def fmt_temp(temp_c):
    """Format temperature showing both C and F."""
    if temp_c is None:
        return "N/A"
    temp_f = round(temp_c * 9 / 5 + 32)
    return f"{round(temp_c)}C/{temp_f}F"
One thing to notice: the scanner covers 35+ cities across every continent. Wellington in New Zealand, Buenos Aires in Argentina, Lucknow in India, multiple Chinese cities. Polymarket's temperature markets are global, and so is this scanner. Each city's forecast comes from the nearest weather station data in Open-Meteo's network.

Step 3: Scraping Polymarket for Tomorrow's Markets
This is where the scanner figures out what to scan. It hits the Polymarket weather/temperature page and scrapes every event slug matching the pattern highest-temperature-in-[city]-on-[date]. Then it filters for tomorrow's date only. We don't care about markets that already resolved or markets that are a week out — we want tomorrow, where the forecast is most accurate and the trading window is tightest.

The date slug format is specific: march-27-2026. The function generates tomorrow's date in this exact format and uses it as a filter. If Polymarket hasn't posted tomorrow's markets yet (they sometimes lag), the scanner will report zero markets and retry on the next cycle.

Scraping Polymarket for temperature slugs
python
Click to copy
def get_tomorrow_date_slug():
    """Get tomorrow's date in the slug format: month-day-year."""
    tomorrow = datetime.now() + timedelta(days=1)
    month = tomorrow.strftime("%B").lower()
    day = str(tomorrow.day)
    year = str(tomorrow.year)
    return f"{month}-{day}-{year}"

def scrape_temperature_slugs():
    """Scrape the Polymarket weather/temperature page for event slugs."""
    url = "https://polymarket.com/weather/temperature"
    tomorrow_slug = get_tomorrow_date_slug()

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    }
    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code != 200:
        return []

    # Find all event slugs matching the temperature pattern
    pattern = r'highest-temperature-in-[\w-]+-on-[\w-]+'
    all_slugs = list(set(re.findall(pattern, resp.text)))

    # Filter for tomorrow only
    tomorrow_slugs = [s for s in all_slugs if s.endswith(tomorrow_slug)]
    return tomorrow_slugs

def extract_city_from_slug(slug):
    """Extract city name from slug like 'highest-temperature-in-shanghai-on-march-25-2026'."""
    match = re.match(r'highest-temperature-in-([\w-]+)-on-', slug)
    if match:
        return match.group(1)
    return None
The regex pattern highest-temperature-in-[\w-]+-on-[\w-]+ is intentionally broad. It catches all temperature slugs on the page regardless of city or date, and the date filtering happens afterward. This makes the scraper resilient to page layout changes — as long as Polymarket keeps the same slug format, the scanner works.

Join tomorrow's live Zoom call here

Step 4: Fetching Market Data from the Gamma API
Once we have the slugs, we need the actual market data — prices, volumes, and bucket breakdowns. Polymarket's public Gamma API returns this in a clean JSON format. Each temperature event has multiple "markets" (buckets), each representing a temperature range. For example, a city might have buckets for "17C", "18C", "19C", and so on.

Each bucket has a YES price (what people are paying for that outcome) and a NO price. The YES price effectively represents the market's estimated probability for that temperature range. If the "22C" bucket has a YES price of $0.35, the market thinks there's a 35% chance the high will be 22C. The scanner identifies two key signals: the market favorite (highest YES price) and the hottest bucket (most 24h volume).

Fetching and parsing market data
python
Click to copy
def fetch_event_data(slug):
    """Fetch event data from Polymarket gamma API."""
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    resp = requests.get(url, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if not data:
        return None
    return data[0] if isinstance(data, list) else data

def parse_market_data(event_data):
    """Parse event data into structured market info."""
    markets = event_data.get("markets", [])
    if not markets:
        return None

    total_volume = 0
    volume_24h = 0
    buckets = []

    for m in markets:
        vol = float(m.get("volume", 0) or 0)
        vol24 = float(m.get("volume24hr", 0) or 0)
        total_volume += vol
        volume_24h += vol24

        # Parse outcome prices - format: "[yes_price, no_price]"
        prices_str = m.get("outcomePrices", "[]")
        if isinstance(prices_str, str):
            prices = json.loads(prices_str)
        else:
            prices = prices_str

        yes_price = float(prices[0]) if len(prices) > 0 else 0
        no_price = float(prices[1]) if len(prices) > 1 else 0

        bucket_title = m.get("groupItemTitle", "Unknown")

        buckets.append({
            "title": bucket_title,
            "yes_price": yes_price,
            "no_price": no_price,
            "volume": vol,
            "volume_24h": vol24,
            "token_id": m.get("clobTokenIds", ""),
        })

    buckets.sort(key=lambda b: b["title"])

    # Market favorite = bucket with highest YES price
    favorite = max(buckets, key=lambda b: b["yes_price"])
    # Hottest bucket = most 24h volume
    hottest = max(buckets, key=lambda b: b["volume_24h"])

    return {
        "total_volume": total_volume,
        "volume_24h": volume_24h,
        "buckets": buckets,
        "num_buckets": len(buckets),
        "favorite_bucket": favorite["title"],
        "favorite_price": favorite["yes_price"],
        "hottest_bucket": hottest["title"],
        "hottest_bucket_vol": hottest["volume_24h"],
        "slug": event_data.get("slug", ""),
    }
Pay attention to the outcomePrices parsing. Polymarket returns prices as a JSON string like "[0.35, 0.65]" — note the quotes around the array. The parser handles both string and direct array formats defensively. This kind of API quirk is exactly the thing that silently breaks scrapers if you're not careful.

Step 5: Fetching Weather Forecasts from Open-Meteo
This is the core of the edge. Open-Meteo is a completely free weather API — no API key, no sign-up, no rate limit headaches. You send it latitude, longitude, and what data you want, and it returns forecasts including hourly temperatures, daily highs and lows, and even ensemble model outputs.

The scanner fetches two types of forecasts for each city. First, the standard forecast which gives the daily max/min and hourly temperatures. Second, the ensemble forecast which runs the same city through 51 independent weather models and returns all 51 predictions. The ensemble is powerful — instead of trusting one model's opinion, you get a probability distribution of what 51 models think the temperature will be.

Both forecasts get cached to disk for one hour. The caching system writes to a JSON file so the cache persists across restarts. If you kill the scanner and restart it 20 minutes later, it won't re-fetch forecasts that are still fresh.

Weather caching system
python
Click to copy
_weather_cache = {}

def _load_cache():
    """Load weather cache from disk if it exists and is fresh."""
    global _weather_cache
    if os.path.exists(WEATHER_CACHE_FILE):
        with open(WEATHER_CACHE_FILE, 'r') as f:
            _weather_cache = json.load(f)
    return _weather_cache

def _save_cache():
    """Save weather cache to disk."""
    with open(WEATHER_CACHE_FILE, 'w') as f:
        json.dump(_weather_cache, f)

def _get_cached(key):
    """Get cached value if fresh (< 1 hour old)."""
    if not _weather_cache:
        _load_cache()
    entry = _weather_cache.get(key)
    if entry and time.time() - entry.get("_ts", 0) < WEATHER_CACHE_MAX_AGE:
        return entry.get("data")
    return None

def _set_cached(key, data):
    """Cache a value with timestamp."""
    _weather_cache[key] = {"data": data, "_ts": time.time()}
    _save_cache()
Standard forecast fetch
python
Click to copy
def fetch_forecast(city_slug):
    """Fetch weather forecast from Open-Meteo for a city (cached for 1 hour)."""
    cached = _get_cached(f"forecast_{city_slug}")
    if cached is not None:
        return cached

    if city_slug not in CITY_COORDS:
        return None

    lat, lon = CITY_COORDS[city_slug]
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "timezone": "auto",
        "forecast_days": 3,
    }
    resp = requests.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=15)
    if resp.status_code != 200:
        return None
    forecast_data = resp.json()

    # Extract tomorrow's hourly temps
    hourly_times = forecast_data.get("hourly", {}).get("time", [])
    hourly_temps = forecast_data.get("hourly", {}).get("temperature_2m", [])

    tomorrow_hourly = []
    for t, temp in zip(hourly_times, hourly_temps):
        if t.startswith(tomorrow_str):
            tomorrow_hourly.append({"time": t, "temp": temp})

    # Extract daily max/min for tomorrow
    daily_times = forecast_data.get("daily", {}).get("time", [])
    daily_max = forecast_data.get("daily", {}).get("temperature_2m_max", [])
    daily_min = forecast_data.get("daily", {}).get("temperature_2m_min", [])

    forecast_high = None
    forecast_low = None
    for i, d in enumerate(daily_times):
        if d == tomorrow_str:
            forecast_high = daily_max[i] if i < len(daily_max) else None
            forecast_low = daily_min[i] if i < len(daily_min) else None
            break

    # Compute max from hourly (more precise)
    hourly_max = max([h["temp"] for h in tomorrow_hourly]) if tomorrow_hourly else None

    # Use hourly max as the "real" forecast high
    if hourly_max is not None:
        forecast_high = hourly_max

    result = {
        "high": forecast_high,
        "low": forecast_low,
        "hourly": tomorrow_hourly,
        "hourly_max": hourly_max,
        "timezone": forecast_data.get("timezone", "unknown"),
    }
    _set_cached(f"forecast_{city_slug}", result)
    return result
Notice that the scanner requests 3 forecast days but only uses tomorrow's data. This is intentional — Open-Meteo's minimum forecast window is 1 day, and requesting 3 days costs nothing extra while giving us a safety margin if the timezone math shifts which "day" we're looking at. The hourly max is preferred over the daily max because hourly data is interpolated at each hour, giving a more precise peak temperature estimate.

Join tomorrow's live Zoom call here

Step 6: Ensemble Forecasts — 51 Models, One Edge
This is the secret weapon. Most people check one weather app and call it a day. The ensemble endpoint gives you predictions from 51 independent weather models (using the ICON Seamless model). For each model member, the scanner calculates the maximum temperature it predicts for tomorrow, giving you a distribution of 51 different "what the high might be" answers.

Why does this matter? Because when 45 out of 51 models say the high will be 22C and the market's favorite bucket is 19C, that's not a weather app hunch — that's a statistically grounded signal. The ensemble distribution gives you confidence. If the models are split 50/50 between 20C and 22C, you know there's genuine uncertainty. If they're clustered tightly around one number, the forecast is confident and a market disagreement is a stronger signal.

Ensemble forecast fetch
python
Click to copy
def fetch_ensemble(city_slug):
    """Fetch ensemble forecast (51 models) from Open-Meteo for probability estimation."""
    cached = _get_cached(f"ensemble_{city_slug}")
    if cached is not None:
        return cached

    if city_slug not in CITY_COORDS:
        return None

    lat, lon = CITY_COORDS[city_slug]
    tomorrow = datetime.now() + timedelta(days=1)
    tomorrow_str = tomorrow.strftime("%Y-%m-%d")

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m",
        "timezone": "auto",
        "forecast_days": 3,
        "models": "icon_seamless",
    }
    resp = requests.get("https://api.open-meteo.com/v1/ensemble", params=params, timeout=15)
    if resp.status_code != 200:
        return None
    data = resp.json()

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])

    # Collect all ensemble member temps for tomorrow
    member_keys = [k for k in hourly.keys() if k.startswith("temperature_2m_member")]
    if not member_keys:
        return None

    tomorrow_maxes = []
    for mk in member_keys:
        member_temps = hourly[mk]
        tomorrow_temps = []
        for t, temp in zip(times, member_temps):
            if t.startswith(tomorrow_str) and temp is not None:
                tomorrow_temps.append(temp)
        if tomorrow_temps:
            tomorrow_maxes.append(max(tomorrow_temps))

    if not tomorrow_maxes:
        return None

    result = {
        "member_maxes": sorted(tomorrow_maxes),
        "ensemble_mean": sum(tomorrow_maxes) / len(tomorrow_maxes),
        "ensemble_min": min(tomorrow_maxes),
        "ensemble_max": max(tomorrow_maxes),
        "num_members": len(tomorrow_maxes),
    }
    _set_cached(f"ensemble_{city_slug}", result)
    return result
The ensemble data gets rendered as a histogram in the detailed city view — showing you something like "22C: 78% (40/51 models)" — a visual probability distribution built from 51 independent forecasts. This is the same kind of probabilistic thinking that quantitative hedge funds use, applied to a prediction market anyone can trade on.

Step 7: Edge Detection — Where Forecast Meets Market
The display logic is where everything comes together. The scanner ranks all cities by 24-hour volume (because that's where liquidity is), then compares each city's forecast to its market favorite. The key function here is parse_bucket_temp which handles something tricky: some Polymarket temperature buckets are in Celsius, some are in Fahrenheit. The parser detects which unit the bucket uses and always converts to Celsius so the comparison against the forecast is apples-to-apples.

The edge scoring is simple and deliberate: 0 degrees off means the forecast agrees with the market (no edge), 1 degree off is a yellow flag (small potential edge), and 2+ degrees off is a red flag (potential mispricing). The scanner shows this for every city in the summary table and does a deep dive for the top 10 by volume.

Bucket temperature parsing and edge detection
python
Click to copy
def parse_bucket_temp(bucket_title):
    """
    Parse temperature from bucket title and ALWAYS return in Celsius.
    Handles both C buckets (e.g. '17C') and F buckets (e.g. '88-89F').
    Converts F to C so we can compare apples to apples with the forecast.
    """
    is_fahrenheit = 'F' in bucket_title or 'f' in bucket_title

    nums = re.findall(r'\d+', bucket_title)
    if not nums:
        return None

    temp = int(nums[0])

    # If it's a range like "88-89F", take the average
    if len(nums) >= 2:
        temp = (int(nums[0]) + int(nums[1])) // 2

    # Convert F to C if needed
    if is_fahrenheit:
        temp = round((temp - 32) * 5 / 9)

    return temp

def _get_forecast_high(fc):
    """Extract high temp from forecast (handles dict or float from cache)."""
    if fc is None:
        return None
    if isinstance(fc, dict):
        return fc.get("high") or fc.get("hourly_max")
    return fc
The Fahrenheit detection is critical. Some cities (especially US cities like Miami, Dallas, Houston) have their Polymarket buckets denominated in Fahrenheit while the forecast comes back in Celsius. Without this conversion, you'd be comparing 22C to "72F" and flagging a 50-degree edge that doesn't exist. This is the kind of subtle bug that would cost you money if you traded on the raw numbers.

Step 8: The Main Scanner Loop
The main loop ties everything together. Each scan cycle follows a clear pipeline: scrape Polymarket for tomorrow's slugs, fetch market data and weather forecasts for each city (with a polite 0.3-second delay between API calls), sort everything by 24-hour volume, and display the results. The top 10 cities by volume get the full detailed breakdown including every bucket price, the hourly forecast, and the ensemble distribution.

The number one city by volume gets an extra "deep dive" analysis that calculates the Moon Dev Edge Score — a simple metric showing how many degrees the forecast disagrees with the market's favorite bucket. When both the standard forecast AND the ensemble mean disagree with the market by 2+ degrees, that's a signal worth investigating.

Main scan function
python
Click to copy
def run_scan():
    """Run one full scan cycle."""
    clear_screen()
    print_header()

    # Step 1: Find tomorrow's markets
    tomorrow_slugs = scrape_temperature_slugs()

    if not tomorrow_slugs:
        print(colored("  No temperature markets for tomorrow found!", "red"))
        print(colored("  Markets may not be posted yet. Will retry next cycle.", "yellow"))
        return []

    # Step 2 & 3: Fetch market data + forecasts for each city
    _load_cache()
    cached_count = sum(1 for s in tomorrow_slugs
                      if _get_cached(f"forecast_{extract_city_from_slug(s)}") is not None)

    city_data = []
    total = len(tomorrow_slugs)

    for idx, slug in enumerate(tomorrow_slugs, 1):
        city = extract_city_from_slug(slug)
        if not city:
            continue

        # Fetch market data
        event_data = fetch_event_data(slug)
        if not event_data:
            continue

        market = parse_market_data(event_data)
        if not market:
            continue

        # Fetch weather forecast
        forecast = fetch_forecast(city)

        # Fetch ensemble (for detailed analysis)
        ensemble = None
        if city in CITY_COORDS:
            ensemble = fetch_ensemble(city)

        city_data.append({
            "city": city,
            "market": market,
            "forecast": forecast,
            "ensemble": ensemble,
        })

        # Be nice to APIs
        time.sleep(0.3)

    # Sort by 24h volume descending
    city_data.sort(key=lambda x: x["market"]["volume_24h"], reverse=True)

    # Print summary table
    print_summary_table(city_data)

    # Print detailed view for top N
    for i, cd in enumerate(city_data[:TOP_N_DETAILED], 1):
        print_detailed_city(cd, i)

    # Print full analysis for #1
    if city_data:
        print_top1_full_analysis(city_data[0])

    return city_data

def main():
    """Main loop - Moon Dev's Weather Scanner."""
    while True:
        run_scan()

        # Countdown to next refresh
        remaining = REFRESH_MINUTES * 60
        while remaining > 0:
            mins = remaining // 60
            secs = remaining % 60
            time.sleep(min(COUNTDOWN_INTERVAL, remaining))
            remaining -= COUNTDOWN_INTERVAL

if __name__ == "__main__":
    main()
That 0.3-second sleep between API calls is there for a reason. When you're hitting 35+ cities in a row — each requiring a Gamma API call, a forecast call, and an ensemble call — you're making 100+ HTTP requests per scan. The sleep keeps you from getting rate-limited and keeps the free Open-Meteo API happy. Being a good API citizen means the free tools stay free.

Running the Scanner
No API keys needed. No .env file. Just install two dependencies and run it.

Install dependencies and run
python
Click to copy
pip install requests termcolor
python weather_scanner.py
The scanner will immediately scrape Polymarket, fetch forecasts for every city, and print a ranked summary table showing all cities by volume with edge flags. The top 10 cities get a full breakdown with every bucket price, hourly forecasts, and ensemble probability distributions. It refreshes every 30 minutes automatically.

If you see a city with a "2C EDGE" or higher in the summary, click through to the Polymarket link in the detailed view and check the bucket prices yourself. Compare the forecast high to the market's favorite bucket. If 45 out of 51 ensemble models agree with the forecast and the market is pricing a different temperature — that's about as close to a free lunch as prediction markets get.

Join tomorrow's live Zoom call here

Wrapping Up
This scanner is a clean example of the kind of edge that code gives you. The data is free. The weather models are public. Polymarket is open to anyone. The only barrier is writing the code that connects these dots — and now you've got it. The scanner finds markets where the crowd disagrees with 51 professional weather models, ranks them by liquidity so you're looking at tradeable opportunities, and refreshes automatically so you don't have to babysit it.

Some ideas for extending it: add historical weather accuracy analysis (how often does the forecast miss by 2+ degrees for each city?), connect it to Polymarket's trading API for automated execution, or add alerts when a new high-edge market appears. The foundation is here — build on it.

Come hang out on the live Zoom calls where we build tools like this together. Ask questions, share what you're working on, and learn from the community. The edge is in the data — and the community is how you find it faster.

Want to build this live with us?
We walk through tools like this on our live Zoom calls. Come hang out, ask questions, and build alongside the Moon Dev community.

Join the Live Zoom Call
Related Resources
Polymarket 5-Minute Bot
HIP3 Funding Rate Scanner
Moon Dev API Documentation
Polymarket Redemption Bot
Built with love by Moon Dev

