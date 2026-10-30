"""
A candidate's region for the fairness report, at state / country level only - never the city. From
the location the resume states (the parse's `location`): an Indian state or union territory named in
it, a known Indian city mapped to its state, or a country named in it. Anything else is "Unknown":
the city list is limited (major cities only), so Unknown includes smaller places.
"""
import re

INDIAN_STATES = {
    "andhra pradesh": "Andhra Pradesh", "arunachal pradesh": "Arunachal Pradesh", "assam": "Assam",
    "bihar": "Bihar", "chhattisgarh": "Chhattisgarh", "goa": "Goa", "gujarat": "Gujarat", "haryana": "Haryana",
    "himachal pradesh": "Himachal Pradesh", "jharkhand": "Jharkhand", "karnataka": "Karnataka", "kerala": "Kerala",
    "madhya pradesh": "Madhya Pradesh", "maharashtra": "Maharashtra", "manipur": "Manipur",
    "meghalaya": "Meghalaya", "mizoram": "Mizoram", "nagaland": "Nagaland", "odisha": "Odisha", "orissa": "Odisha",
    "punjab": "Punjab", "rajasthan": "Rajasthan", "sikkim": "Sikkim", "tamil nadu": "Tamil Nadu",
    "telangana": "Telangana", "tripura": "Tripura", "uttar pradesh": "Uttar Pradesh", "uttarakhand": "Uttarakhand",
    "west bengal": "West Bengal", "andaman and nicobar islands": "Andaman and Nicobar Islands",
    "chandigarh": "Chandigarh", "dadra and nagar haveli and daman and diu": "Dadra and Nagar Haveli and Daman and Diu",
    "delhi": "Delhi", "new delhi": "Delhi", "jammu and kashmir": "Jammu and Kashmir", "ladakh": "Ladakh",
    "lakshadweep": "Lakshadweep", "puducherry": "Puducherry", "pondicherry": "Puducherry",
}
CITY_STATE = {
    "mumbai": "Maharashtra", "bombay": "Maharashtra", "pune": "Maharashtra", "nagpur": "Maharashtra",
    "nashik": "Maharashtra", "thane": "Maharashtra", "navi mumbai": "Maharashtra", "aurangabad": "Maharashtra",
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "mysuru": "Karnataka", "mysore": "Karnataka",
    "mangaluru": "Karnataka", "mangalore": "Karnataka", "hubli": "Karnataka",
    "chennai": "Tamil Nadu", "madras": "Tamil Nadu", "coimbatore": "Tamil Nadu", "madurai": "Tamil Nadu",
    "tiruchirappalli": "Tamil Nadu", "trichy": "Tamil Nadu", "salem": "Tamil Nadu",
    "hyderabad": "Telangana", "secunderabad": "Telangana", "warangal": "Telangana",
    "visakhapatnam": "Andhra Pradesh", "vizag": "Andhra Pradesh", "vijayawada": "Andhra Pradesh",
    "guntur": "Andhra Pradesh", "tirupati": "Andhra Pradesh",
    "kochi": "Kerala", "cochin": "Kerala", "thiruvananthapuram": "Kerala", "trivandrum": "Kerala",
    "kozhikode": "Kerala", "calicut": "Kerala", "thrissur": "Kerala",
    "kolkata": "West Bengal", "calcutta": "West Bengal", "howrah": "West Bengal", "durgapur": "West Bengal",
    "noida": "Uttar Pradesh", "greater noida": "Uttar Pradesh", "lucknow": "Uttar Pradesh", "kanpur": "Uttar Pradesh",
    "ghaziabad": "Uttar Pradesh", "varanasi": "Uttar Pradesh", "agra": "Uttar Pradesh", "prayagraj": "Uttar Pradesh",
    "allahabad": "Uttar Pradesh", "gurugram": "Haryana", "gurgaon": "Haryana", "faridabad": "Haryana",
    "ahmedabad": "Gujarat", "surat": "Gujarat", "vadodara": "Gujarat", "baroda": "Gujarat", "rajkot": "Gujarat",
    "gandhinagar": "Gujarat", "jaipur": "Rajasthan", "jodhpur": "Rajasthan", "udaipur": "Rajasthan",
    "kota": "Rajasthan", "indore": "Madhya Pradesh", "bhopal": "Madhya Pradesh", "gwalior": "Madhya Pradesh",
    "jabalpur": "Madhya Pradesh", "patna": "Bihar", "gaya": "Bihar", "ranchi": "Jharkhand",
    "jamshedpur": "Jharkhand", "dhanbad": "Jharkhand", "bhubaneswar": "Odisha", "cuttack": "Odisha",
    "raipur": "Chhattisgarh", "guwahati": "Assam", "dehradun": "Uttarakhand", "shimla": "Himachal Pradesh",
    "ludhiana": "Punjab", "amritsar": "Punjab", "jalandhar": "Punjab", "mohali": "Punjab",
    "srinagar": "Jammu and Kashmir", "jammu": "Jammu and Kashmir", "imphal": "Manipur", "shillong": "Meghalaya",
    "aizawl": "Mizoram", "kohima": "Nagaland", "agartala": "Tripura", "gangtok": "Sikkim", "itanagar": "Arunachal Pradesh",
    "panaji": "Goa",
}
COUNTRIES = {
    "india": "India", "sri lanka": "Sri Lanka", "bangladesh": "Bangladesh", "nepal": "Nepal", "pakistan": "Pakistan",
    "bhutan": "Bhutan", "maldives": "Maldives", "united arab emirates": "United Arab Emirates",
    "uae": "United Arab Emirates", "saudi arabia": "Saudi Arabia", "qatar": "Qatar", "oman": "Oman",
    "kuwait": "Kuwait", "bahrain": "Bahrain", "singapore": "Singapore", "malaysia": "Malaysia",
    "united kingdom": "United Kingdom", "uk": "United Kingdom", "england": "United Kingdom",
    "united states": "United States", "usa": "United States", "us": "United States", "canada": "Canada",
    "australia": "Australia", "new zealand": "New Zealand", "germany": "Germany", "france": "France",
    "netherlands": "Netherlands", "ireland": "Ireland", "japan": "Japan", "china": "China",
    "south africa": "South Africa", "kenya": "Kenya", "nigeria": "Nigeria",
}
UNKNOWN = "Unknown"


def _key(text: str) -> str:
    return re.sub(r"[^a-z ]+", " ", text.lower()).strip()


def region_of(location: str | None) -> str:
    """'<State>, India', '<Country>', or 'Unknown' - never a city."""
    if not location or not str(location).strip():
        return UNKNOWN
    parts = [_key(p) for p in re.split(r"[,/|;]", str(location)) if _key(p)]
    for part in reversed(parts):                       # the broadest part is usually last
        if part in INDIAN_STATES:
            return f"{INDIAN_STATES[part]}, India"
    for part in reversed(parts):
        if part in COUNTRIES and COUNTRIES[part] != "India":
            return COUNTRIES[part]
    for part in parts:
        if part in CITY_STATE:
            return f"{CITY_STATE[part]}, India"
    if any(part in COUNTRIES for part in parts):
        return "India"                                 # "India" alone, no state known
    return UNKNOWN
