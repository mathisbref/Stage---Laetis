from feuille_route.models import Tournee, Etape
from producteurs.models import SiteProduction
from .models import StatistiqueTrajet

from datetime import datetime, timedelta
from collections import defaultdict
from statistics import stdev



# ---------------------  Travail de Mathis ----------------------- #



def liste_trajets(params):
    donnees_trajets = []
    donnees_moy_temps = []
    durations = defaultdict(list)  # Stocke les durées des trajets (lieuDep, lieuArr) -> [durées]
    prods = []

    # Extraction des paramètres
    p = params.split('&')
    p1 = p[0]
    if len(p)>1:
        p2 = p[1]
        prods = p2[6:].split('~')
    else:
        prods.append("")

    date_d, date_f = [datetime.strptime(d, "%Y-%m-%d").date() for d in p1[6:].split('~')]
    

    # Récupération des tournées dans l'intervalle de dates
    tournees_q = Tournee.objects.filter(date__range=[date_d, date_f])

    for t in tournees_q:
        etapes = list(Etape.objects.filter(tournee_id=t.id).order_by("ordre"))
        etapes_list = []
        trajet_encours = None  

        for i in range(len(etapes)):
            etape = etapes[i]
            lieu = etape.get_lieu()
            heure = etape.heure.strftime("%H:%M:%S") if etape.heure else "Non défini"

            if etape.lieu_type in ["collecte", "collecte_c", "delester", "recollecte"]:
                if trajet_encours:
                    trajet_encours["lieuArr"] = lieu.nom
                    trajet_encours["heureArr"] = heure

                    if trajet_encours["heureDep"] != "Non défini" and heure != "Non défini":
                        duree = datetime.combine(t.date, etape.heure) - datetime.combine(
                            t.date, datetime.strptime(trajet_encours["heureDep"], "%H:%M:%S").time()
                        )
                        trajet_encours["duree"] = str(duree)

                        # Stocker la durée dans le dictionnaire
                        durations[(trajet_encours["lieuDep"], trajet_encours["lieuArr"])].append(duree)

                    else:
                        trajet_encours["duree"] = "Non défini"

                    etapes_list.append(trajet_encours)

                trajet_encours = {
                    "id": etape.id,
                    "ordre": etape.ordre,
                    "lieuDep": lieu.nom,
                    "heureDep": heure,
                    "lieuArr": "",
                    "heureArr": "",
                    "duree": "",
                }

        if trajet_encours and trajet_encours["lieuArr"]:
            etapes_list.append(trajet_encours)

        donnees_trajets.append({
            "id": t.id,
            "date": t.date.strftime("%Y-%m-%d"),
            "etapes": etapes_list
        })

    # Parcourir les sites et ajouter leur info + moyennes des trajets
    if prods[0] != "":
        for site_id in prods:
            site_production = SiteProduction.objects.filter(id_public=site_id).first()

            if site_production:
                moyennes_trajets = []

                # Calculer la moyenne des durées pour chaque destination
                for (lieuDep, lieuArr), durees in durations.items():
                    if lieuDep == site_production.nom:
                        # Convertir les durées en minutes float
                        durees_minutes = [d.total_seconds() / 60 for d in durees]

                        duree_moyenne = sum(durees, timedelta()) / len(durees)
                        temps_min = min(durees)
                        temps_max = max(durees)

                        # Calcul de l'écart-type (si au moins 2 valeurs)
                        ecart_type = round(stdev(durees_minutes), 2) if len(durees_minutes) > 1 else 0.0

                        moyennes_trajets.append({
                            "dest": lieuArr,
                            "duree_m": round(duree_moyenne.total_seconds() / 60),
                            "freq": len(durees),
                            "temps_min": str(round(temps_min.total_seconds() / 60)),
                            "temps_max": str(round(temps_max.total_seconds() / 60)),
                            "ecart_type": ecart_type
                        })

                        # Create Update StatistiqueTrajet
                        StatistiqueTrajet.objects.update_or_create(
                            lieu_depart=lieuDep,
                            lieu_arrivee=lieuArr,
                            site_production=site_production,
                            defaults={
                                "duree_moyenne": round(duree_moyenne.total_seconds() / 60),
                                "frequence": len(durees),
                                "temps_min": round(temps_min.total_seconds() / 60),
                                "temps_max": round(temps_max.total_seconds() / 60),
                                "ecart_type": ecart_type,
                                
                            }
                        )
                        


                donnees_moy_temps.append({
                    "id": site_production.id_public,
                    "nom": site_production.nom,
                    "surnom": site_production.nom[:3],
                    "moyenne_trajet": moyennes_trajets
                })

    return donnees_trajets, donnees_moy_temps

