from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

from bs4 import BeautifulSoup
import re
import time

def scrape_tracker_with_selenium(url: str):
    options = Options()
    options.headless = True
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.get(url)

    time.sleep(5)  # Let the page fully render

    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    # Parse match info
    map_el = soup.select_one(".match-header .map")
    match_map = map_el.text.strip() if map_el else "Unknown"

    date_el = soup.select_one(".match-header .date")
    match_date = date_el.text.strip() if date_el else "Unknown"

    players = []
    teams = soup.select(".scoreboard .team")
    for team in teams:
        for row in team.select(".scoreboard__row"):
            name = row.select_one(".player__name")
            agent = row.select_one(".agent img")
            kda = row.select(".player__stats .value")

            try:
                kills = int(kda[0].text.strip())
                deaths = int(kda[1].text.strip())
                assists = int(kda[2].text.strip())
                kda_text = f"{kills}/{deaths}/{assists}"
            except:
                kda_text = "?/?/?"

            adr_el = row.find("div", string=re.compile("ADR"))
            adr = adr_el.find_next_sibling("div").text.strip() if adr_el else "0"

            hs_el = row.find("div", string=re.compile("HS%"))
            hs_pct = hs_el.find_next_sibling("div").text.strip().replace('%', '') if hs_el else "0"

            score_el = row.find("div", string=re.compile("ACS"))
            score = score_el.find_next_sibling("div").text.strip() if score_el else "0"

            players.append({
                "name": name.text.strip() if name else "Unknown",
                "agent": agent['alt'] if agent and 'alt' in agent.attrs else "Unknown",
                "kda": kda_text,
                "adr": int(adr),
                "hs_pct": float(hs_pct),
                "score": int(score),
            })

    return {
        "map": match_map,
        "date": match_date,
        "players": players
    }

# Example
if __name__ == "__main__":
    url = "https://tracker.gg/valorant/match/b7042395-2557-4b78-9927-aead3837a89f"
    result = scrape_tracker_with_selenium(url)
    print(result)
