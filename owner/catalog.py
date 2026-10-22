"""Makes and models for the "add a car" suggestions (popular models in India; any other make or model can be typed in)."""
from __future__ import annotations

MODELS = {
    "Tata": ["Altroz", "Curvv", "Harrier", "Nexon", "Nexon EV", "Punch", "Punch EV", "Safari", "Tiago", "Tiago EV", "Tigor"],
    "Maruti Suzuki": ["Alto K10", "Baleno", "Brezza", "Celerio", "Dzire", "Ertiga", "Fronx", "Grand Vitara", "Swift", "Wagon R", "XL6"],
    "Hyundai": ["Alcazar", "Aura", "Creta", "Exter", "Grand i10 Nios", "i20", "Venue", "Verna"],
    "Mahindra": ["Bolero", "Scorpio-N", "Thar", "XUV 3XO", "XUV700", "BE 6", "XEV 9e"],
    "Kia": ["Carens", "Seltos", "Sonet", "Syros"],
    "Toyota": ["Fortuner", "Glanza", "Innova Crysta", "Innova Hycross", "Urban Cruiser Hyryder"],
    "Honda": ["Amaze", "City", "Elevate"],
    "MG": ["Astor", "Comet EV", "Hector", "Windsor EV", "ZS EV"],
    "Renault": ["Kiger", "Kwid", "Triber"],
    "Skoda": ["Kushaq", "Kylaq", "Slavia"],
    "Volkswagen": ["Taigun", "Virtus"],
    "Nissan": ["Magnite"],
}


def catalog() -> dict:
    return {"makes": [{"make": m, "models": v} for m, v in MODELS.items()],
            "fuels": ["petrol", "diesel", "cng", "electric", "hybrid"]}
