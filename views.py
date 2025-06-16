...

# --------- Mathis -----------
from supervision.helpers_tournees import (liste_trajets)

...


# --------------------------------------------------------------  Travail de Mathis ---------------------------------------------------------- #

@permission_required("users.acces_supervision")
def matrice_pts_collecte_moteur(request):
    producteurs_json = json.dumps(liste_prods(), cls=DjangoJSONEncoder)

    ctx = {"producteurs_json": producteurs_json} 
    return render(request, "supervision/matrice-pts-collecte-moteur.html", ctx)

@permission_required("users.acces_supervision")
def matrice_pts_collecte(request, params):
    tab1,tab2 = liste_trajets(params)
    ctx = {
        "params": params,
        "donnees_json": json.dumps({"trajets":tab1,"moyennes":tab2}, cls=DjangoJSONEncoder),
    }
    return render(request, "supervision/matrice-pts-collecte.html", ctx)

