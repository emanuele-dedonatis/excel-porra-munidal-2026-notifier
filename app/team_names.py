import unicodedata

# Spanish name (as used in the Excel template) -> English name (as returned by football-data.org)
SPANISH_TO_ENGLISH: dict[str, str] = {
    # CONMEBOL
    "Argentina": "Argentina",
    "Brasil": "Brazil",
    "Uruguay": "Uruguay",
    "Colombia": "Colombia",
    "Ecuador": "Ecuador",
    "Venezuela": "Venezuela",
    "Chile": "Chile",
    "Bolivia": "Bolivia",
    "Perú": "Peru",
    "Peru": "Peru",
    "Paraguay": "Paraguay",
    # UEFA
    "Alemania": "Germany",
    "Francia": "France",
    "España": "Spain",
    "Inglaterra": "England",
    "Portugal": "Portugal",
    "Países Bajos": "Netherlands",
    "Bélgica": "Belgium",
    "Italia": "Italy",
    "Croacia": "Croatia",
    "Dinamarca": "Denmark",
    "Polonia": "Poland",
    "Serbia": "Serbia",
    "Ucrania": "Ukraine",
    "Hungría": "Hungary",
    "Austria": "Austria",
    "Escocia": "Scotland",
    "Turquía": "Turkey",
    "Suiza": "Switzerland",
    "Suecia": "Sweden",
    "Noruega": "Norway",
    "Grecia": "Greece",
    "Eslovenia": "Slovenia",
    "Eslovaquia": "Slovakia",
    "Rumanía": "Romania",
    "Rumania": "Romania",
    "Chequia": "Czechia",
    "República Checa": "Czechia",
    "Bosnia y Herzegovina": "Bosnia-Herzegovina",
    "Albania": "Albania",
    "Georgia": "Georgia",
    "Gales": "Wales",
    # CONCACAF
    "México": "Mexico",
    "Estados Unidos": "United States",
    "Canadá": "Canada",
    "Costa Rica": "Costa Rica",
    "Panamá": "Panama",
    "Jamaica": "Jamaica",
    "Honduras": "Honduras",
    "El Salvador": "El Salvador",
    "Guatemala": "Guatemala",
    "Trinidad y Tobago": "Trinidad and Tobago",
    "Haití": "Haiti",
    "Cuba": "Cuba",
    # CAF
    "Marruecos": "Morocco",
    "Egipto": "Egypt",
    "Senegal": "Senegal",
    "Nigeria": "Nigeria",
    "Sudáfrica": "South Africa",
    "Argelia": "Algeria",
    "Costa de Marfil": "Ivory Coast",
    "Ghana": "Ghana",
    "Camerún": "Cameroon",
    "Túnez": "Tunisia",
    "Mali": "Mali",
    "R.D. Congo": "Congo DR",
    "RD Congo": "Congo DR",
    "Tanzania": "Tanzania",
    "Angola": "Angola",
    "Zambia": "Zambia",
    "Uganda": "Uganda",
    # AFC
    "Japón": "Japan",
    "Corea del Sur": "South Korea",
    "Australia": "Australia",
    "Irán": "Iran",
    "Arabia Saudí": "Saudi Arabia",
    "Arabia Saudita": "Saudi Arabia",
    "Catar": "Qatar",
    "Qatar": "Qatar",
    "Uzbekistán": "Uzbekistan",
    "China": "China",
    "Irak": "Iraq",
    "Iraq": "Iraq",
    "Emiratos Árabes Unidos": "United Arab Emirates",
    "Kuwait": "Kuwait",
    "Jordania": "Jordan",
    "Indonesia": "Indonesia",
    "Curazao": "Curaçao",
    "Cabo Verde": "Cape Verde Islands",
    # OFC
    "Nueva Zelanda": "New Zealand",
}

ENGLISH_TO_SPANISH: dict[str, str] = {v: k for k, v in SPANISH_TO_ENGLISH.items()}


def normalize(name: str) -> str:
    """Lowercase and strip accents for fuzzy comparison."""
    name = name.lower().strip()
    return "".join(
        c for c in unicodedata.normalize("NFD", name)
        if unicodedata.category(c) != "Mn"
    )


def spanish_to_english(name: str) -> str:
    return SPANISH_TO_ENGLISH.get(name, name)


def names_match(spanish_name: str, english_name: str) -> bool:
    """Return True if a Spanish Excel team name maps to an English API team name."""
    mapped = normalize(spanish_to_english(spanish_name))
    target = normalize(english_name)
    return mapped == target or mapped in target or target in mapped
