"""Build the committed reference-data CSVs.

Provenance for `backend/src/concordance/data/reference/*.csv`. The generator
itself never holds name lists inline - it reads these files - so this script is
the one place the raw vocabulary lives. Re-run it only to extend a table:

    python scripts/build_reference_data.py

Frequencies follow a Zipf law over a hand-ordered list, which is the point: the
EM fit at Stage 3 learns nothing interesting from a uniform name distribution,
because the whole signal in a surname agreement is how rare that surname is.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "backend" / "src" / "concordance" / "data" / "reference"

# Ordered most-common first; weights are assigned by a Zipf law below.
SURNAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell", "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans", "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes", "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers", "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey", "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward", "Richardson", "Watson", "Brooks", "Chavez", "Wood", "James", "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez", "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster", "Jimenez", "Powell", "Jenkins", "Perry", "Russell", "Sullivan", "Bell", "Coleman", "Butler", "Henderson", "Barnes", "Gonzales", "Fisher", "Vasquez", "Simmons", "Romero", "Jordan", "Patterson", "Alexander", "Hamilton", "Graham", "Reynolds", "Griffin", "Wallace", "Moreno", "West", "Cole", "Hayes", "Bryant", "Herrera", "Gibson", "Ellis", "Tran", "Medina", "Aguilar", "Stevens", "Murray", "Ford", "Castro", "Marshall", "Owens", "Harrison", "Fernandez", "Mcdonald", "Woods", "Washington", "Kennedy", "Wells", "Vargas", "Henry", "Chen", "Freeman", "Webb", "Tucker", "Guzman", "Burns", "Crawford", "Olson", "Simpson", "Porter", "Hunter", "Gordon", "Mendez", "Silva", "Shaw", "Snyder", "Mason", "Dixon", "Munoz", "Hunt", "Hicks", "Holmes", "Palmer", "Wagner", "Black", "Robertson", "Boyd", "Rose", "Stone", "Salazar", "Fox", "Warren", "Mills", "Meyer", "Rice", "Schmidt", "Garza", "Daniels", "Ferguson", "Nichols", "Stephens", "Soto", "Weaver", "Ryan", "Gardner", "Payne", "Grant", "Dunn", "Kelley", "Spencer", "Hawkins", "Arnold", "Pierce", "Vazquez", "Hansen", "Peters", "Santos", "Hart", "Bradley", "Knight", "Elliott", "Cunningham", "Duncan", "Armstrong", "Hudson", "Carroll", "Lane", "Riley", "Andrews", "Alvarado", "Ray", "Delgado", "Berry", "Perkins", "Hoffman", "Johnston", "Matthews", "Pena", "Richards", "Contreras", "Willis", "Carpenter", "Lawrence", "Sandoval", "Guerrero", "George", "Chapman", "Rios", "Estrada", "Ortega", "Watkins", "Greene", "Nunez", "Wheeler", "Valdez", "Harper", "Burke", "Larson", "Santiago", "Maldonado", "Morrison", "Franklin", "Carlson", "Austin", "Dominguez", "Carr", "Lawson", "Jacobs", "Obrien", "Lynch", "Singh", "Vega", "Bishop", "Montgomery", "Oliver", "Jensen", "Harvey", "Williamson", "Gilbert", "Dean", "Sims", "Espinoza", "Howell", "Li", "Wong", "Reid", "Hanson", "Le", "Mccoy", "Garrett", "Burton", "Fuller", "Wang", "Weber", "Welch", "Rojas", "Lucas", "Marquez", "Fields", "Park", "Yang", "Little", "Banks", "Padilla", "Day", "Walsh", "Bowman", "Schultz", "Luna", "Fowler", "Mejia", "Davidson", "Brewer", "May", "Holland", "Juarez", "Newman", "Pearson", "Curtis", "Cortez", "Douglas", "Schneider", "Joseph", "Barrett", "Navarro", "Figueroa", "Keller", "Avila", "Wade", "Molina", "Stanley", "Hopkins", "Campos", "Barnett", "Bates", "Chambers", "Caldwell", "Beck", "Lambert", "Miranda", "Byrd", "Craig", "Ayala", "Lowe", "Frazier", "Powers", "Neal", "Leonard", "Gregory", "Carrillo", "Sutton", "Fleming", "Rhodes", "Shelton", "Schwartz", "Norris", "Jennings", "Watts", "Duran", "Walters", "Cohen", "Mcdaniel", "Moran", "Parks", "Steele", "Vaughn", "Becker", "Holt", "Deleon", "Barker", "Terry", "Hale", "Leon", "Hail", "Benson", "Haynes", "Horton", "Miles", "Lyons", "Pham", "Graves", "Bush", "Thornton", "Wolfe", "Warner", "Cabrera", "Mckinney", "Mann", "Zimmerman", "Dawson", "Lara", "Fletcher", "Page", "Mccarthy", "Love", "Robles", "Cervantes", "Solis", "Erickson", "Reeves", "Chang", "Klein", "Salinas", "Fuentes", "Baldwin", "Daniel", "Simon", "Velasquez", "Hardy", "Higgins", "Aguirre", "Lin", "Cummings", "Chandler", "Sharp", "Barber", "Bowen", "Ochoa", "Dennis", "Robbins", "Liu", "Ramsey", "Francis", "Griffith", "Paul", "Blair", "Oconnor", "Cardenas", "Pacheco", "Cross", "Calderon", "Quinn", "Moss", "Swanson", "Chan", "Rivas", "Khan", "Rodgers", "Serrano", "Fitzgerald", "Rosales", "Stevenson", "Christensen", "Manning", "Gill", "Curry", "Mclaughlin", "Harmon", "Mcgee", "Gross", "Dobbs", "Zamora", "Dillon", "Kaur", "Mcguire", "Bradford", "Grimes", "Novak", "Prescott", "Ashford", "Winslow", "Thorne", "Ellison", "Kirkland", "Marsden", "Ackerman", "Vandyke", "Oyelaran", "Nakamura", "Petrov", "Almeida"]

GIVEN_MALE = {
    "1945": "Robert John James William Richard Charles Ronald Donald Joseph Thomas Gerald Larry",
    "1955": "Michael David Gary Steven Mark Dennis Kenneth Bruce Danny Roger Wayne Glenn",
    "1965": "John Brian Kevin Jeffrey Timothy Gregory Scott Todd Craig Keith Randy Dale",
    "1975": "Christopher Jason Matthew Eric Sean Chad Shawn Troy Derek Travis Corey Brent",
    "1985": "Joshua Andrew Justin Ryan Brandon Nicholas Adam Aaron Nathan Jeremy Cory Dustin",
    "1995": "Tyler Zachary Austin Dylan Cody Jordan Kyle Alex Hunter Caleb Ethan Trevor",
}

GIVEN_FEMALE = {
    "1945": "Mary Patricia Barbara Linda Carol Nancy Sandra Sharon Judith Betty Shirley Joyce",
    "1955": "Deborah Susan Karen Donna Cynthia Diane Janet Kathleen Brenda Pamela Debra Cheryl",
    "1965": "Lisa Michelle Kimberly Laura Theresa Julie Denise Tammy Lori Dawn Tracy Rhonda",
    "1975": "Jennifer Amy Melissa Heather Angela Stephanie Christina Nicole Rebecca Kelly Erin Shannon",
    "1985": "Ashley Jessica Amanda Sarah Brittany Megan Lauren Danielle Katherine Rachel Courtney Tiffany",
    "1995": "Emily Hannah Alexis Samantha Taylor Madison Kayla Victoria Olivia Alyssa Jasmine Destiny",
}

NICKNAMES = ["Robert,Bob", "Robert,Bobby", "Robert,Rob", "Robert,Bert", "William,Bill", "William,Billy", "William,Will", "William,Willie", "Richard,Rick", "Richard,Dick", "Richard,Richie", "Richard,Rich", "Margaret,Peggy", "Margaret,Maggie", "Margaret,Meg", "Margaret,Marge", "Elizabeth,Beth", "Elizabeth,Liz", "Elizabeth,Betty", "Elizabeth,Eliza", "James,Jim", "James,Jimmy", "James,Jamie", "John,Jack", "John,Johnny", "Michael,Mike", "Michael,Mickey", "Charles,Chuck", "Charles,Charlie", "Joseph,Joe", "Joseph,Joey", "Thomas,Tom", "Thomas,Tommy", "Christopher,Chris", "Christopher,Topher", "Anthony,Tony", "Nicholas,Nick", "Steven,Steve", "Stephen,Steve", "Daniel,Dan", "Daniel,Danny", "Matthew,Matt", "Andrew,Andy", "Andrew,Drew", "David,Dave", "Donald,Don", "Ronald,Ron", "Kenneth,Ken", "Edward,Ed", "Edward,Ted", "Lawrence,Larry", "Gregory,Greg", "Jeffrey,Jeff", "Timothy,Tim", "Benjamin,Ben", "Samuel,Sam", "Alexander,Alex", "Zachary,Zach", "Nathaniel,Nate", "Frederick,Fred", "Patricia,Pat", "Patricia,Patty", "Patricia,Trish", "Barbara,Barb", "Barbara,Babs", "Susan,Sue", "Susan,Susie", "Deborah,Debbie", "Deborah,Deb", "Katherine,Kathy", "Katherine,Kate", "Katherine,Katie", "Jennifer,Jen", "Jennifer,Jenny", "Cynthia,Cindy", "Sandra,Sandy", "Kimberly,Kim", "Rebecca,Becky", "Rebecca,Becca", "Theresa,Terry", "Christina,Chris", "Christina,Tina", "Victoria,Vicky", "Danielle,Dani", "Samantha,Sam", "Stephanie,Steph", "Michelle,Shelly", "Pamela,Pam", "Angela,Angie", "Veronica,Ronnie", "Josephine,Josie", "Eleanor,Ellie", "Virginia,Ginny", "Dorothy,Dot"]

CREDENTIALS = [
    ("MD", "degree"), ("DO", "degree"), ("DDS", "degree"), ("DMD", "degree"),
    ("DPM", "degree"), ("DC", "degree"), ("OD", "degree"), ("PharmD", "degree"),
    ("PhD", "degree"), ("RN", "licence"), ("LPN", "licence"), ("NP", "licence"),
    ("APRN", "licence"), ("PA", "licence"), ("PA-C", "licence"), ("CRNA", "licence"),
    ("LCSW", "licence"), ("PT", "licence"), ("OT", "licence"), ("RPh", "licence"),
    ("FACS", "fellowship"), ("FACP", "fellowship"), ("FAAP", "fellowship"),
    ("Jr", "generational"), ("Sr", "generational"), ("II", "generational"),
    ("III", "generational"), ("IV", "generational"),
]

# state, name, population weight (millions, 2020-ish), cities as name:zip3
STATES = [
    ("CA", "California", 39.5, "Los Angeles:900 San Diego:921 San Jose:951 Fresno:937 Sacramento:958"),
    ("TX", "Texas", 29.1, "Houston:770 San Antonio:782 Dallas:752 Austin:787 El Paso:799"),
    ("FL", "Florida", 21.5, "Jacksonville:322 Miami:331 Tampa:336 Orlando:328 Tallahassee:323"),
    ("NY", "New York", 20.2, "New York:100 Buffalo:142 Rochester:146 Syracuse:132 Albany:122"),
    ("PA", "Pennsylvania", 13.0, "Philadelphia:191 Pittsburgh:152 Allentown:181 Erie:165 Harrisburg:171"),
    ("IL", "Illinois", 12.8, "Chicago:606 Aurora:605 Rockford:611 Peoria:616 Springfield:627"),
    ("OH", "Ohio", 11.8, "Columbus:432 Cleveland:441 Cincinnati:452 Toledo:436 Akron:443"),
    ("GA", "Georgia", 10.7, "Atlanta:303 Augusta:309 Savannah:314 Columbus:319 Macon:312"),
    ("NC", "North Carolina", 10.4, "Charlotte:282 Raleigh:276 Greensboro:274 Durham:277 Winston Salem:271"),
    ("MI", "Michigan", 10.0, "Detroit:482 Grand Rapids:495 Lansing:489 Ann Arbor:481 Flint:485"),
    ("NJ", "New Jersey", 9.3, "Newark:071 Jersey City:073 Paterson:075 Trenton:086 Camden:081"),
    ("VA", "Virginia", 8.6, "Virginia Beach:234 Norfolk:235 Richmond:232 Arlington:222 Roanoke:240"),
    ("WA", "Washington", 7.7, "Seattle:981 Spokane:992 Tacoma:984 Vancouver:986 Olympia:985"),
    ("AZ", "Arizona", 7.2, "Phoenix:850 Tucson:857 Mesa:852 Flagstaff:860 Yuma:853"),
    ("MA", "Massachusetts", 7.0, "Boston:021 Worcester:016 Springfield:011 Cambridge:021 Lowell:018"),
    ("TN", "Tennessee", 6.9, "Nashville:372 Memphis:381 Knoxville:379 Chattanooga:374 Clarksville:370"),
    ("IN", "Indiana", 6.8, "Indianapolis:462 Fort Wayne:468 Evansville:477 South Bend:466 Bloomington:474"),
    ("MD", "Maryland", 6.2, "Baltimore:212 Columbia:210 Silver Spring:209 Rockville:208 Annapolis:214"),
    ("MO", "Missouri", 6.2, "Kansas City:641 Saint Louis:631 Springfield:658 Columbia:652 Joplin:648"),
    ("WI", "Wisconsin", 5.9, "Milwaukee:532 Madison:537 Green Bay:543 Kenosha:531 Appleton:549"),
    ("CO", "Colorado", 5.8, "Denver:802 Colorado Springs:809 Aurora:800 Fort Collins:805 Pueblo:810"),
    ("MN", "Minnesota", 5.7, "Minneapolis:554 Saint Paul:551 Rochester:559 Duluth:558 Bloomington:554"),
    ("SC", "South Carolina", 5.1, "Charleston:294 Columbia:292 Greenville:296 Rock Hill:297 Florence:295"),
    ("AL", "Alabama", 5.0, "Birmingham:352 Montgomery:361 Mobile:366 Huntsville:358 Tuscaloosa:354"),
    ("LA", "Louisiana", 4.6, "New Orleans:701 Baton Rouge:708 Shreveport:711 Lafayette:705 Monroe:712"),
    ("KY", "Kentucky", 4.5, "Louisville:402 Lexington:405 Bowling Green:421 Owensboro:423 Covington:410"),
    ("OR", "Oregon", 4.2, "Portland:972 Salem:973 Eugene:974 Medford:975 Bend:977"),
    ("OK", "Oklahoma", 4.0, "Oklahoma City:731 Tulsa:741 Norman:730 Lawton:735 Enid:737"),
    ("CT", "Connecticut", 3.6, "Bridgeport:066 New Haven:065 Hartford:061 Stamford:069 Waterbury:067"),
    ("UT", "Utah", 3.3, "Salt Lake City:841 Provo:846 Ogden:844 Saint George:847 Logan:843"),
    ("IA", "Iowa", 3.2, "Des Moines:503 Cedar Rapids:524 Davenport:528 Iowa City:522 Sioux City:511"),
    ("NV", "Nevada", 3.1, "Las Vegas:891 Reno:895 Henderson:890 Carson City:897 Sparks:894"),
    ("AR", "Arkansas", 3.0, "Little Rock:722 Fort Smith:729 Fayetteville:727 Jonesboro:724 Hot Springs:719"),
    ("MS", "Mississippi", 3.0, "Jackson:392 Gulfport:395 Hattiesburg:394 Tupelo:388 Meridian:393"),
    ("KS", "Kansas", 2.9, "Wichita:672 Topeka:666 Kansas City:661 Lawrence:660 Salina:674"),
    ("NM", "New Mexico", 2.1, "Albuquerque:871 Las Cruces:880 Santa Fe:875 Roswell:882 Farmington:874"),
    ("NE", "Nebraska", 1.9, "Omaha:681 Lincoln:685 Bellevue:680 Grand Island:688 Kearney:688"),
    ("ID", "Idaho", 1.8, "Boise:837 Idaho Falls:834 Pocatello:832 Coeur dAlene:838 Twin Falls:833"),
    ("WV", "West Virginia", 1.8, "Charleston:253 Huntington:257 Morgantown:265 Parkersburg:261 Wheeling:260"),
    ("HI", "Hawaii", 1.4, "Honolulu:968 Hilo:967 Kailua:967 Kaneohe:967 Waipahu:967"),
    ("NH", "New Hampshire", 1.4, "Manchester:031 Nashua:030 Concord:033 Dover:038 Portsmouth:038"),
    ("ME", "Maine", 1.3, "Portland:041 Lewiston:042 Bangor:044 Augusta:043 Biddeford:040"),
    ("MT", "Montana", 1.1, "Billings:591 Missoula:598 Great Falls:594 Bozeman:597 Helena:596"),
    ("RI", "Rhode Island", 1.1, "Providence:029 Warwick:028 Cranston:029 Pawtucket:028 Newport:028"),
    ("DE", "Delaware", 1.0, "Wilmington:198 Dover:199 Newark:197 Middletown:197 Smyrna:199"),
    ("SD", "South Dakota", 0.9, "Sioux Falls:571 Rapid City:577 Aberdeen:574 Brookings:570 Pierre:575"),
    ("ND", "North Dakota", 0.8, "Fargo:581 Bismarck:585 Grand Forks:582 Minot:587 Williston:588"),
    ("AK", "Alaska", 0.7, "Anchorage:995 Fairbanks:997 Juneau:998 Wasilla:996 Sitka:998"),
    ("VT", "Vermont", 0.6, "Burlington:054 Rutland:057 Montpelier:056 Brattleboro:053 Barre:056"),
    ("WY", "Wyoming", 0.6, "Cheyenne:820 Casper:826 Laramie:820 Gillette:827 Rock Springs:829"),
]

STREET_NAMES = ["Main", "Oak", "Maple", "Park", "Elm", "Washington", "Cedar", "Lake", "Pine", "Walnut", "Spring", "Hill", "Church", "Highland", "Ridge", "Sunset", "Willow", "Adams", "Jefferson", "Lincoln", "Chestnut", "Franklin", "Madison", "Jackson", "Dogwood", "Birch", "Meadow", "Sycamore", "Poplar", "Cherry", "Center", "Union", "Market", "Water", "River", "Broad", "State", "Third", "Fourth", "Fifth", "Prospect", "Summit", "Valley", "Forest", "Garden", "Grove", "Harrison", "Monroe", "Clinton", "Wilson", "Magnolia", "Juniper", "Aspen", "Laurel", "Cypress", "Hickory", "Beech", "Spruce", "Sequoia", "Redwood", "Baker", "Carter", "Bristol", "Dover", "Essex", "Fairview", "Glenwood", "Hawthorne", "Ivy", "Kingston"]

STREET_TYPES = ["Street", "St", "Avenue", "Ave", "Road", "Rd", "Drive", "Dr", "Lane", "Ln", "Boulevard", "Blvd", "Court", "Ct", "Place", "Pl", "Way", "Terrace", "Ter", "Circle", "Cir", "Parkway", "Pkwy"]

SPECIALTIES = [
    "Internal Medicine", "Family Medicine", "Pediatrics", "Emergency Medicine",
    "Anesthesiology", "Psychiatry", "Radiology", "General Surgery",
    "Obstetrics and Gynecology", "Orthopaedic Surgery", "Cardiology", "Dermatology",
    "Neurology", "Ophthalmology", "Otolaryngology", "Urology", "Pathology",
    "Physical Medicine and Rehabilitation", "Gastroenterology", "Pulmonary Disease",
    "Nephrology", "Endocrinology", "Rheumatology", "Hematology and Oncology",
    "Infectious Disease", "Geriatric Medicine", "Neonatology", "Plastic Surgery",
    "Vascular Surgery", "Thoracic Surgery", "Neurological Surgery", "Nurse Practitioner",
    "Physician Assistant", "Registered Nurse", "Clinical Social Work", "Physical Therapy",
    "Occupational Therapy", "Chiropractic", "Podiatry", "Optometry", "Dentistry",
    "Oral Surgery", "Pharmacy", "Clinical Psychology", "Speech Language Pathology",
    "Home Health Agency", "Skilled Nursing Facility", "Durable Medical Equipment",
    "Ambulance Service", "Clinical Laboratory",
]

# kind,value  - assembled into organization names by the generator
ORG_COMPONENTS = [
    ("head", "Riverside"), ("head", "Lakeview"), ("head", "Summit"), ("head", "Pioneer"),
    ("head", "Cornerstone"), ("head", "Evergreen"), ("head", "Heritage"), ("head", "Beacon"),
    ("head", "Parkview"), ("head", "Northgate"), ("head", "Southpoint"), ("head", "Westfield"),
    ("head", "Eastside"), ("head", "Clearwater"), ("head", "Highland"), ("head", "Meridian"),
    ("head", "Sunrise"), ("head", "Cedar Ridge"), ("head", "Granite Bay"), ("head", "Silver Creek"),
    ("head", "Fairmont"), ("head", "Oakhurst"), ("head", "Blue Ridge"), ("head", "Golden Gate"),
    ("head", "Liberty"), ("head", "Trinity"), ("head", "Saint Agnes"), ("head", "Saint Luke"),
    ("head", "Mercy"), ("head", "Providence"), ("head", "Good Samaritan"), ("head", "Mount Vernon"),
    ("core", "Family Practice"), ("core", "Medical"), ("core", "Health"), ("core", "Healthcare"),
    ("core", "Behavioral Health"), ("core", "Home Health"), ("core", "Physical Therapy"),
    ("core", "Diagnostic Imaging"), ("core", "Surgical"), ("core", "Primary Care"),
    ("core", "Urgent Care"), ("core", "Cardiology"), ("core", "Orthopedic"), ("core", "Dialysis"),
    ("core", "Rehabilitation"), ("core", "Dental"), ("core", "Eye Care"), ("core", "Pediatric"),
    ("core", "Womens Health"), ("core", "Oncology"), ("core", "Pharmacy"), ("core", "Laboratory"),
    ("core", "Ambulance"), ("core", "Hospice"), ("core", "Skilled Nursing"),
    ("tail", "Center"), ("tail", "Centers"), ("tail", "Group"), ("tail", "Associates"),
    ("tail", "Partners"), ("tail", "Clinic"), ("tail", "Services"), ("tail", "Specialists"),
    ("tail", "Institute"), ("tail", "Practice"), ("tail", "Network"), ("tail", "Systems"),
    ("suffix", "LLC"), ("suffix", "Inc"), ("suffix", "PC"), ("suffix", "PA"),
    ("suffix", "LLP"), ("suffix", "Corp"), ("suffix", "PLLC"), ("suffix", ""),
]

SANCTION_TYPES = [
    ("Exclusion - Mandatory", 0.28), ("Exclusion - Permissive", 0.16),
    ("License Revocation", 0.12), ("License Suspension", 0.10),
    ("Program Termination", 0.09), ("Debarment", 0.07),
    ("Payment Suspension", 0.06), ("Probation", 0.05),
    ("Reprimand", 0.04), ("Civil Monetary Penalty", 0.03),
]

SOURCE_AUTHORITIES = [
    ("OIG-LEIE", "leie", 0.40),
    ("SAM.gov", "sam", 0.20),
    ("State Medicaid - TX", "state", 0.15),
    ("State Medicaid - NY", "state", 0.13),
    ("State Medical Board - CA", "board", 0.12),
]


def zipf(n: int, s: float = 1.07) -> list[float]:
    """Weights following a Zipf law - the long tail is the point."""
    return [1.0 / ((i + 1) ** s) for i in range(n)]


def write(name: str, header: list[str], rows: list[tuple]) -> None:
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rows)
    print(f"{path.relative_to(OUT.parents[4])}: {len(rows)} rows")


def main() -> None:
    weights = zipf(len(SURNAMES))
    write("surnames.csv", ["surname", "weight"],
          [(n, f"{w:.8f}") for n, w in zip(SURNAMES, weights, strict=True)])

    rows: list[tuple] = []
    for sex, table in (("M", GIVEN_MALE), ("F", GIVEN_FEMALE)):
        for era, names in table.items():
            names_list = names.split()
            for name, w in zip(names_list, zipf(len(names_list), s=0.9), strict=True):
                rows.append((name, sex, era, f"{w:.8f}"))
    write("given_names.csv", ["given_name", "sex", "birth_era", "weight"], rows)

    pairs = [tuple(p.split(",")) for p in NICKNAMES]
    write("nicknames.csv", ["canonical", "nickname"], sorted(set(pairs)))

    write("credentials.csv", ["credential", "kind"], CREDENTIALS)

    write("states.csv", ["state", "state_name", "weight"],
          [(s, n, f"{w:.4f}") for s, n, w, _ in STATES])

    city_rows: list[tuple] = []
    for state, _, _, cities in STATES:
        # City names contain spaces, so match "Name:zip3" pairs rather than split.
        for city, zip3 in re.findall(r"([A-Za-z][A-Za-z ]*):(\d{3})", cities):
            city_rows.append((state, city.strip(), zip3))
    write("cities.csv", ["state", "city", "zip3"], city_rows)

    write("street_names.csv", ["street_name"], [(n,) for n in STREET_NAMES])
    write("street_types.csv", ["street_type"], [(t,) for t in STREET_TYPES])
    write("specialties.csv", ["specialty"], [(s,) for s in SPECIALTIES])
    write("org_components.csv", ["kind", "value"], ORG_COMPONENTS)
    write("sanction_types.csv", ["sanction_type", "weight"],
          [(t, f"{w:.4f}") for t, w in SANCTION_TYPES])
    write("source_authorities.csv", ["source_authority", "dialect", "weight"],
          [(a, d, f"{w:.4f}") for a, d, w in SOURCE_AUTHORITIES])


if __name__ == "__main__":
    main()
