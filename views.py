import collections
import io
import json
from collections import OrderedDict
from datetime import date, datetime
from mimetypes import guess_type

from django.contrib.auth.decorators import permission_required
from django.core import serializers
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from anomalies.forms import (
    AnomalieForm,
    DateAnomalieForm,
    EtatAnomalieForm,
    FichierAnomalieForm,
    SuiviAnomalieForm,
)
from anomalies.models import Anomalie, AnomalieSuivi
from clients.models import Client, LieuLivraison
from clients_facturation.models import VenteLait
from espace_producteur.models import JourPaturage
from outils.models import Doc
from personnes.models import Groupe
from producteurs.helpers import get_campagnes_date
from producteurs.models import (
    CampagneProduction,
    ContratProducteur,
    EntrepriseAgricole,
    EtatEntrepriseAgricole,
    LieuCollecte,
    SiteProduction,
)
from supervision.helpers import (
    generer_tableau_contrats,
    get_bilan_previsions_livraisons,
    get_dict_volumes_contractualises_clients,
    get_dict_volumes_contractualises_contrat,
    get_filtres_visites,
    liste_clients,
    liste_prods,
)
from supervision.helpers_analyses import (
    export_analyses_producteurs,
    get_analyses_producteurs,
)
from supervision.helpers_carte_clients import get_infos_lieux
from supervision.helpers_collectes import (
    get_bilan_collectes,
    get_bilan_collectes_jours,
    get_bilan_delestages,
    get_bilan_previsions_collectes,
    get_prod_volumes_quotidiens_tk,
)
from supervision.helpers_debut_fin_traite import (
    detection_debuts_fins_campagnes,
    entrees_sorties_serializer,
    export_traites,
    fins_campagnes_annuelles,
)
from supervision.helpers_export import (
    export_bilan_activite,
    export_bilan_collectes,
    export_bilan_delestages,
    export_bilan_previsions_collectes,
    export_bilan_previsions_livraisons,
    export_carte_clients_excel,
    export_carte_excel,
    export_contrats_tableau,
    export_emargement_liste,
    export_prevision_activite,
    export_prod_volumes_quotidiens_tk,
)
from supervision.helpers_marges import get_marges
from supervision.helpers_non_conformite import get_creationuser_name
from supervision.helpers_pertes import get_pertes_lait
from supervision.helpers_previsions import get_previsions
from supervision.helpers_tests import export_test_differentielmsu
from supervision.helpers_transport import (
    export_station_stockage_io,
    export_transporteurs_releves,
)
from supervision.helpers_visites import get_bilan_visites

# --------- Mathis -----------
from supervision.helpers_tournees import (liste_trajets)


@permission_required("users.acces_supervision")
def producteurs(request):
    ctx = {}
    ctx["actual_year"] = datetime.now().year
    return render(request, "supervision/producteurs.html", ctx)


@permission_required("users.acces_supervision")
def clients(request):
    ctx = {}
    return render(request, "supervision/clients.html", ctx)


@permission_required("users.acces_supervision")
def debut_fin_traite_moteur(request):
    ctx = {}
    return render(request, "supervision/debut-fin-traite-moteur.html", ctx)


@permission_required("users.acces_supervision")
def debut_fin_traite(request, date_debut_filtre, date_fin_filtre):
    ctx = {}
    ctx["entrees_sorties"] = []

    # ajustement dates du filtre
    date_debut_obj = datetime.strptime(date_debut_filtre, "%Y-%m-%d")
    date_fin_obj = datetime.strptime(date_fin_filtre, "%Y-%m-%d")
    if (
        date_fin_obj > datetime.today()
    ):  # s'arreter a la date du jour, inutile calculer futur
        date_fin_obj = datetime.today()

    # debuts et fins
    ctx["entrees_sorties"] += detection_debuts_fins_campagnes(
        date_debut_obj, date_fin_obj
    )

    # campagnes sur l'annee
    ctx["campagnes_annee"] = (
        CampagneProduction.objects.filter(  # campagne annuelle qui se termine dans periode filtre
            date_fin__gte=date_debut_obj,
            date_fin__lte=date_fin_obj,
            date_fin__month=12,
            date_fin__day=31,
        )
        .exclude(site_production__entreprise_agricole__est_tiers=True)
        .order_by("id")
    )

    ctx["entrees_sorties"] += fins_campagnes_annuelles(
        date_debut_obj, date_fin_obj, ctx["campagnes_annee"]
    )

    # valeurs globales
    campagnes_au_debut = []
    for elem in get_campagnes_date(date_debut_obj):
        campagne = get_object_or_404(CampagneProduction, pk=elem["campagne"]["id"])
        if campagne.site_production.entreprise_agricole.etat.label == "Membre":
            campagnes_au_debut.append(campagne)

    nb_sorties = 0
    nb_entrees = 0
    for elem in ctx["entrees_sorties"]:
        if elem["sortie"]:
            nb_sorties += 1
        if elem["entree"]:
            nb_entrees += 1

    ctx["entrees_sorties"] = sorted(
        ctx["entrees_sorties"], key=lambda item: item["date"]
    )
    ctx["entrees_sorties_json"] = json.dumps(
        ctx["entrees_sorties"], default=entrees_sorties_serializer
    )

    ctx["campagnes_entraite_debut"] = campagnes_au_debut
    ctx["nb_campagnes_sorties"] = nb_sorties
    ctx["nb_campagnes_entrees"] = nb_entrees
    ctx["nb_campagnes_entraite_fin"] = len(campagnes_au_debut) + nb_entrees - nb_sorties
    ctx["date_debut_filtre"] = date_debut_obj
    ctx["date_fin_filtre"] = date_fin_obj

    return render(request, "supervision/debut-fin-traite.html", ctx)


@permission_required("users.acces_supervision")
def debut_fin_traite_export(request):
    ctx = {}
    ctx["date_debut_filtre"] = request.POST.get("date_debut_filtre")
    ctx["date_fin_filtre"] = request.POST.get("date_fin_filtre")
    entrees_sorties_json = request.POST.get("entrees_sorties", "[]")

    try:
        # Désérialiser la chaîne JSON en liste de dictionnaires
        entrees_sorties = json.loads(entrees_sorties_json)
    except json.JSONDecodeError:
        entrees_sorties = []  # En cas d'erreur, retourner une liste vide
    ctx["entrees_sorties"] = entrees_sorties
    return export_traites(ctx)


# liste de toutes las anomalies de tous les clients
@permission_required("users.acces_supervision")
def clients_non_conformites(request):
    ctx = {}

    anomalies = Anomalie.objects.all().order_by("-creation_date")
    ctx["anomalies"] = anomalies
    data = serializers.serialize("json", anomalies)
    ctx["anomalies_json"] = json.dumps(data, cls=DjangoJSONEncoder)
    ctx["statuts"] = Anomalie._meta.get_field("etat").choices
    ctx["priorites"] = Anomalie._meta.get_field("priorite").choices
    ctx["clients"] = Client.objects.all().order_by("id_public")

    clientsjson = serializers.serialize("json", ctx["clients"])
    ctx["clients_json"] = json.dumps(clientsjson, cls=DjangoJSONEncoder)

    dict_priorite = {}
    for priorite in Anomalie.PRIORITES:
        dict_priorite[priorite[0]] = priorite[1]
    ctx["dict_priorite_json"] = json.dumps(dict_priorite)

    dict_statut = {}
    for statut in Anomalie.ETATS:
        dict_statut[statut[0]] = statut[1]
    ctx["dict_statut_json"] = json.dumps(dict_statut)

    return render(request, "supervision/clients-non-conformites.html", ctx)


# suivi d'une anomalie en particulier
@permission_required("users.acces_supervision")
def client_non_conformite(request, pk):

    ctx = {}
    ctx["pk"] = pk

    ctx["anomalie"] = get_object_or_404(Anomalie, pk=pk)

    # affecte nom/prenom a anomalie.creation_user
    if (
        not ctx["anomalie"].creation_user.first_name
        and not ctx["anomalie"].creation_user.last_name
    ):
        try:
            ctx["anomalie"].creation_user.first_name = get_creationuser_name(
                ctx["anomalie"].creation_user
            ).prenom
            ctx["anomalie"].creation_user.last_name = get_creationuser_name(
                ctx["anomalie"].creation_user
            ).nom
        except:
            pass

    ctx["fichiers"] = ctx["anomalie"].fichiers.all()

    # liste des suivis de l'anomalie
    ctx["suivisanomalies"] = AnomalieSuivi.objects.filter(anomalie__pk=pk).order_by(
        "date"
    )

    # formulaires
    ctx["anomalie_form"] = AnomalieForm(request.POST or None, instance=ctx["anomalie"])
    ctx["date_anomalie_form"] = DateAnomalieForm(
        request.POST or None, instance=ctx["anomalie"]
    )
    ctx["etat_anomalie_form"] = EtatAnomalieForm(
        request.POST or None, instance=ctx["anomalie"]
    )
    ctx["suivianomalie_form"] = SuiviAnomalieForm(request.POST or None)
    ctx["fichieranomalie_form"] = FichierAnomalieForm(
        request.POST or None, request.FILES or None
    )
    if request.method == "POST":
        # formulaire objet anomalie
        if "modifier_objet_anomalie" in request.POST:
            if ctx["anomalie_form"].is_valid():
                anomalie = ctx["anomalie_form"].save(commit=False)
                anomalie.save()

        # formulaire date anomalie
        elif "modifier_date_anomalie" in request.POST:
            if ctx["date_anomalie_form"].is_valid():
                anomalie = ctx["date_anomalie_form"].save(commit=False)
                anomalie.save()

        # formulaire etat anomalie
        elif "etat_anomalie" in request.POST:
            if ctx["etat_anomalie_form"].is_valid():
                anomalie = ctx["etat_anomalie_form"].save(commit=False)
                anomalie.save()

        # formulaire suivi anomalie
        elif "ajouter_suivi_anomalie" in request.POST:
            if ctx["suivianomalie_form"].is_valid():
                suivi = ctx["suivianomalie_form"].save(commit=False)
                suivi.anomalie = ctx["anomalie"]
                suivi.creation_user = request.user
                # affecte nom/prenom a suivi.creation_user
                if (
                    not suivi.creation_user.first_name
                    and not suivi.creation_user.last_name
                ):
                    try:
                        suivi.creation_user.first_name = get_creationuser_name(
                            suivi.creation_user
                        ).prenom
                        suivi.creation_user.last_name = get_creationuser_name(
                            suivi.creation_user
                        ).nom
                    except:
                        pass
                suivi.save()
                ctx["suivianomalie_form"] = SuiviAnomalieForm(None)  # clear form

        # formulaire fichier anomalie
        elif "fichier_anomalie" in request.POST:
            if ctx["fichieranomalie_form"].is_valid():
                fichier = ctx["fichieranomalie_form"].save(commit=False)
                fichier.type = "anomalie_fichier_joint"
                fichier.save()
                ctx["anomalie"].fichiers.add(fichier)
                ctx["fichieranomalie_form"] = FichierAnomalieForm()
            else:
                ctx["fichieranomalie_form"] = FichierAnomalieForm()

    return render(request, "supervision/client-non-conformite.html", ctx)


def anomalie_file(request, pk, fpk, filename):
    doc = get_object_or_404(Doc, pk=fpk)
    return HttpResponse(doc.file, content_type=guess_type(doc.file.name)[0])


def anomalie_file_delete(request, pk, fpk):
    doc = get_object_or_404(Doc, pk=fpk)
    doc.delete()
    return redirect("supervision:client-non-conformite", pk)


@permission_required("users.acces_supervision")
def emargement_filtres(request):
    ctx = {}
    ctx["groupes"] = collections.OrderedDict()
    ctx["groupes"] = Groupe.objects.all().order_by("ordre")
    ctx["today_date"] = datetime.today().strftime("%d-%m-%Y")

    return render(request, "supervision/emargement_filtres.html", ctx)


@permission_required("users.acces_supervision")
def emargement_liste(request):
    ctx = {}
    ctx["checkboxes_selected"] = request.GET.getlist("options[]") + request.GET.getlist(
        "groupes_options[]"
    )

    # clients
    ctx["clients"] = {}
    clients = list(Client.objects.all().order_by("id_public", "raison_sociale"))
    for client in clients:
        ctx["clients"][client.raison_sociale] = {
            "id_public_client": None,
            "personnels": {},
        }
        if client.id_public:
            ctx["clients"][client.raison_sociale]["id_public_client"] = client.id_public
        i = 1
        for personnel in client.contacts.all():
            ctx["clients"][client.raison_sociale]["personnels"][personnel] = {
                "dernier": False,
            }
            if i == len(client.contacts.all()):
                ctx["clients"][client.raison_sociale]["personnels"][personnel][
                    "dernier"
                ] = True
                i = 1
            else:
                i += 1

    # groupes
    ctx["groupes_checkboxes_selected"] = request.GET.getlist("groupes_options[]")
    ctx["groupes_selectionnes"] = {}
    groupes = Groupe.objects.filter(nom__in=ctx["groupes_checkboxes_selected"])

    liste_all_personnes = []  # liste d'objets Personne pour tester doublons

    # membres des groupes selectionnes
    for groupe in groupes:
        ctx["groupes_selectionnes"][groupe] = {}
        membres = groupe.membre.all().order_by("id")
        for membre in membres:
            if membre.personne not in liste_all_personnes:  # test pour eviter doublon
                if membre is membres.last():
                    ctx["groupes_selectionnes"][groupe][membre] = {
                        "dernier": True,
                    }
                else:
                    ctx["groupes_selectionnes"][groupe][membre] = {
                        "dernier": False,
                    }
                liste_all_personnes.append(membre.personne)

    # date format fr
    date = request.GET.get("date_ag")
    if date is not None and date != "":
        split_date = date.split("-")
        ctx["date_ag"] = split_date[2] + "/" + split_date[1] + "/" + split_date[0]

    # producteurs membres
    entreprises = EntrepriseAgricole.objects.filter(etat=1).order_by(
        "site_production__id_public"
    )

    # creation tab de tab
    ctx["donnees_entreprises"] = collections.OrderedDict()
    for entreprise in entreprises:
        ctx["donnees_entreprises"][entreprise.raison_sociale] = {
            "id_public_entreprise": None,
            "personnels": {},
        }

        # definir dernier personnel de l'entreprise pour tracer separation dans tableau html
        allpersonnel = entreprise.personnes.all().order_by("id")
        for personnel in allpersonnel:
            if personnel not in liste_all_personnes:  # test pour eviter doublon
                ctx["donnees_entreprises"][entreprise.raison_sociale]["personnels"][
                    personnel
                ] = {
                    "dernier": False,
                }
                if personnel.id is allpersonnel.last().id:
                    ctx["donnees_entreprises"][entreprise.raison_sociale]["personnels"][
                        personnel
                    ]["dernier"] = True
                liste_all_personnes.append(personnel)

    # affectations donnees pour chaque entreprise
    for entreprise in entreprises:
        ctx["donnees_entreprises"][entreprise.raison_sociale][
            "id_public_entreprise"
        ] = entreprise.site_production.id_public

    return render(request, "supervision/emargement_liste.html", ctx)


@permission_required("users.acces_supervision")
def emargement_liste_export(request, params):
    dest_filename = "emargement_{}.xlsx".format(datetime.now().strftime("%d_%m_%Y"))
    return export_emargement_liste(request, dest_filename, params)


@permission_required("users.acces_supervision")
def carte_producteurs(request):
    ctx = {}
    annee_n_2 = datetime.today().year - 2
    annee_n = datetime.today().year

    start_n2 = date(datetime.now().year - 2, 1, 1)
    end_n2 = date(datetime.now().year - 2, 12, 31)
    start_n1 = date(datetime.now().year - 1, 1, 1)
    end_n1 = date(datetime.now().year - 1, 12, 31)
    start_n = date(datetime.now().year, 1, 1)
    end_n = datetime.now()
    # total L vendus annee N-2
    ventes_n2 = VenteLait.objects.filter(facture__date__range=[start_n2, end_n2])
    total_ventes_n2 = 0
    for vente in ventes_n2:
        total_ventes_n2 += vente.litrage_total
    # total L vendus annee N-1
    ventes_n1 = VenteLait.objects.filter(facture__date__range=[start_n1, end_n1])
    total_ventes_n1 = 0
    for vente in ventes_n1:
        total_ventes_n1 += vente.litrage_total
    # total L vendus annee N
    ventes_n = VenteLait.objects.filter(facture__date__range=[start_n, end_n])
    total_ventes_n = 0
    for vente in ventes_n:
        total_ventes_n += vente.litrage_total

    groupes_annees = (
        OrderedDict()
    )  # dictionnaire de liste: groupes_annees[annee]=[nb_fermes de cette annee, nb_fermes_total, litrage_annee]
    groupes_annees[annee_n_2] = [0, 0, total_ventes_n2]
    groupes_annees[annee_n_2][0] = 0
    groupes_annees[datetime.today().year - 1] = [0, 0, total_ventes_n1]
    groupes_annees[datetime.today().year - 1][0] = 0
    groupes_annees[annee_n] = [0, 0, total_ventes_n]
    groupes_annees[annee_n][0] = 0
    groupes_annees[datetime.today().year + 1] = [0, 0, 0]
    groupes_annees[datetime.today().year + 1][0] = 0
    groupes_annees[datetime.today().year + 2] = [0, 0, 0]
    groupes_annees[datetime.today().year + 2][0] = 0

    ctx["annee_n_2"] = annee_n_2
    ctx["annee_n"] = annee_n

    compteur_elements_groupes = collections.OrderedDict()

    lieux_collecte = LieuCollecte.objects.filter(
        site_production__entreprise_agricole__etat=1
    ).order_by("site_production__id_public")

    marker = collections.OrderedDict()

    for lieu in lieux_collecte:
        if lieu.site_production:
            marker[lieu.site_production.entreprise_agricole.raison_sociale] = {
                "gps": None,
                "id_entreprise": lieu.site_production.entreprise_agricole.entreprise_ptr_id,
                "id_public_entreprise": None,
                "annee": None,
                "hide": False,
            }

            if lieu.site_production.entreprise_agricole.date_entree_gie and lieu.gps:

                marker[lieu.site_production.entreprise_agricole.raison_sociale][
                    "gps"
                ] = lieu.gps
                marker[lieu.site_production.entreprise_agricole.raison_sociale][
                    "id_public_entreprise"
                ] = lieu.site_production.id_public

                # affectation des annees aux markers + regroupement markers annees n-2
                if (
                    lieu.site_production.entreprise_agricole.date_entree_gie.year
                    <= annee_n_2
                ):
                    marker[lieu.site_production.entreprise_agricole.raison_sociale][
                        "annee"
                    ] = annee_n_2
                    groupes_annees[annee_n_2][0] += 1
                    if (
                        groupes_annees[annee_n_2][0] > 4
                    ):  # cacher nombreux elements liste
                        marker[lieu.site_production.entreprise_agricole.raison_sociale][
                            "hide"
                        ] = True
                else:
                    marker[lieu.site_production.entreprise_agricole.raison_sociale][
                        "annee"
                    ] = lieu.site_production.entreprise_agricole.date_entree_gie.year
                    if (
                        lieu.site_production.entreprise_agricole.date_entree_gie.year
                        in groupes_annees
                    ):
                        groupes_annees[
                            lieu.site_production.entreprise_agricole.date_entree_gie.year
                        ][0] += 1
                    if (
                        groupes_annees[
                            lieu.site_production.entreprise_agricole.date_entree_gie.year
                        ][0]
                        > 4
                    ):  # cacher nombreux elements liste
                        marker[lieu.site_production.entreprise_agricole.raison_sociale][
                            "hide"
                        ] = True

    # nb_fermes_total par annee
    groupes_annees[annee_n_2][1] = groupes_annees[annee_n_2][0]
    groupes_annees[datetime.today().year - 1][1] = (
        groupes_annees[annee_n_2][1] + groupes_annees[datetime.today().year - 1][0]
    )
    groupes_annees[annee_n][1] = (
        groupes_annees[datetime.today().year - 1][1] + groupes_annees[annee_n][0]
    )
    groupes_annees[datetime.today().year + 1][1] = (
        groupes_annees[annee_n][1] + groupes_annees[datetime.today().year + 1][0]
    )
    groupes_annees[datetime.today().year + 2][1] = (
        groupes_annees[datetime.today().year + 1][1]
        + groupes_annees[datetime.today().year + 2][0]
    )

    ctx["groupes"] = groupes_annees
    ctx["groupes_annees"] = json.dumps(groupes_annees)
    ctx["markers"] = json.dumps(marker, cls=DjangoJSONEncoder)
    ctx["markers_html"] = marker
    ctx["tailles_groupes"] = compteur_elements_groupes
    ctx["donnees_entreprises"] = json.dumps(
        generer_tableau_contrats(True), cls=DjangoJSONEncoder
    )

    return render(request, "supervision/carte-producteurs.html", ctx)


@permission_required("users.acces_supervision")
def carte_clients(request):
    ctx = {}
    lieux = []
    lieux_livraison = []
    clients = Client.objects.all().order_by("id_public")

    for client in clients:
        try:
            lieu_livraison = LieuLivraison.objects.filter(
                client=client, ordre=1, archive=False
            ).first()
            if lieu_livraison.gps:
                lieux_livraison.append(lieu_livraison)
        except:  # client pas de lieu de livraison
            pass

    start_n1 = date(datetime.now().year - 1, 1, 1)
    end_n1 = date(datetime.now().year - 1, 12, 31)
    for lieu in lieux_livraison:
        total_litrage_c_n1 = 0
        ventes_c_n1 = VenteLait.objects.filter(
            contrat__client=lieu.client, facture__date__range=[start_n1, end_n1]
        )
        for vente in ventes_c_n1:
            # total litres pour trier clients
            total_litrage_c_n1 += vente.litrage_total
        lieux.append(
            {
                "nom_client": lieu.client.raison_sociale,
                "id_client": lieu.client.id,
                "id_public_client": lieu.client.id_public,
                "gps": lieu.gps,
                "volume_n1": total_litrage_c_n1,
            }
        )

    # tri des clients par volume achete N-1
    sort_clients = sorted(lieux, key=lambda x: x["volume_n1"], reverse=True)

    ctx["dict_infos_lieux"] = json.dumps(
        get_infos_lieux(lieux_livraison), cls=DjangoJSONEncoder
    )
    ctx["lieux_json"] = json.dumps(lieux, cls=DjangoJSONEncoder)
    ctx["lieux"] = sort_clients
    ctx["clients_obj"] = serializers.serialize("json", clients)
    return render(request, "supervision/carte-clients.html", ctx)


@permission_required("users.acces_supervision")
def carte_clients_export_kml(request):
    clients = Client.objects.all().order_by("id_public")
    f = io.BytesIO()
    f.write(b"<?xml version='1.0' encoding='UTF-8'?>\n")
    f.write(b"<kml xmlns='http://www.opengis.net/kml/2.2'>\n")
    f.write(b"<Document>\n")
    for client in clients:
        try:
            lieu_livraison = LieuLivraison.objects.filter(
                client=client, ordre=1, archive=False
            ).first()
            if lieu_livraison.gps:
                positions = (lieu_livraison.gps).split(",")
                f.write(b"\t<Placemark>\n")
                f.write(b"\t\t<name>")
                f.write(str(client.id_public).encode())
                f.write(b" ")
                f.write((client.raison_sociale).encode())
                f.write(b"</name>\n")
                # f.write("\t\t<description>" + str(lieu) + "</description>\n")
                f.write(b"\t\t<Point>\n")
                f.write(b"\t\t\t<coordinates>")
                f.write(positions[1].replace(" ", "").encode())
                f.write(b",")
                f.write(positions[0].replace(" ", "").encode())
                f.write(b",0</coordinates>\n")
                f.write(b"\t\t</Point>\n")
                f.write(b"\t</Placemark>\n")
        except:  # client pas de lieu de livraison
            pass
    f.write(b"</Document>\n")
    f.write(b"</kml>\n")
    f.seek(0)
    response = HttpResponse(content=f, content_type="application/kml")
    response["Content-Disposition"] = (
        'attachment; filename="carte-clients_aveyron-brebis-bio_'
        + str(date.today())
        + '.kml"'
    )
    return response


@permission_required("users.acces_supervision")
def carte_clients_export_excel(request):
    return export_carte_clients_excel(
        '"carte-clients_aveyron-brebis-bio_' + str(date.today()) + '.xlsx"'
    )


@permission_required("users.acces_supervision")
def carte_export_kml(request):
    lieux_collecte = LieuCollecte.objects.filter(
        Q(site_production__entreprise_agricole__etat__id=1)
        | Q(site_production__entreprise_agricole__etat__id=3)
    ).order_by("site_production__id_public")
    f = io.BytesIO()
    f.write(b"<?xml version='1.0' encoding='UTF-8'?>\n")
    f.write(b"<kml xmlns='http://www.opengis.net/kml/2.2'>\n")
    f.write(b"<Document>\n")
    for lieu in lieux_collecte:
        if lieu.site_production and lieu.gps:
            positions = (lieu.gps).split(",")
            f.write(b"\t<Placemark>\n")
            f.write(b"\t\t<name>")
            f.write(str(lieu.site_production.id_public).encode())
            f.write(b" ")
            f.write((lieu.site_production.entreprise_agricole.raison_sociale).encode())
            f.write(b"</name>\n")
            # f.write("\t\t<description>" + str(lieu) + "</description>\n")
            f.write(b"\t\t<Point>\n")
            f.write(b"\t\t\t<coordinates>")
            f.write(positions[1].replace(" ", "").encode())
            f.write(b",")
            f.write(positions[0].replace(" ", "").encode())
            f.write(b",0</coordinates>\n")
            f.write(b"\t\t</Point>\n")
            f.write(b"\t</Placemark>\n")
    f.write(b"</Document>\n")
    f.write(b"</kml>\n")
    f.seek(0)
    response = HttpResponse(content=f, content_type="application/kml")
    response["Content-Disposition"] = (
        'attachment; filename="carte-producteurs_aveyron-brebis-bio_'
        + str(date.today())
        + '.kml"'
    )
    return response


@permission_required("users.acces_supervision")
def carte_export_excel(request):
    return export_carte_excel(
        '"carte-producteurs_aveyron-brebis-bio_' + str(date.today()) + '.xlsx"'
    )


@permission_required("users.acces_supervision")
def dashboard(request):
    ctx = {}
    return render(request, "supervision/dashboard.html", ctx)


@permission_required("users.acces_supervision")
def contrats_filtres(request):
    ctx = {}
    ctx["etats"] = EtatEntrepriseAgricole.objects.all()
    return render(request, "supervision/contrats_filtres.html", ctx)


@permission_required("users.acces_supervision")
def contrats_tableau(request):
    ctx = {}
    ctx["id_selected"] = request.GET.getlist("filtres")
    ctx["checkboxes_selected"] = EtatEntrepriseAgricole.objects.filter(
        id__in=ctx["id_selected"]
    )
    ctx["donnees_entreprises"] = generer_tableau_contrats(False)

    return render(request, "supervision/contrats_tableau.html", ctx)


@permission_required("users.acces_supervision")
def contrats_tableau_export(request, params):
    for r in (("[", ""), ("]", ""), ("'", ""), (" ", "")):
        params = params.replace(*r)
    id_selected = [int(x) for x in params.split(",")]
    etats = EtatEntrepriseAgricole.objects.filter(id__in=id_selected)
    labels = []
    for etat in etats:
        labels.append(str(etat.label))
    dest_filename = "contrats_producteurs.xlsx"

    return export_contrats_tableau(labels, dest_filename)


@permission_required("users.acces_supervision")
def annuaire_filtres(request):
    ctx = {}
    ctx["etats"] = EtatEntrepriseAgricole.objects.all()
    return render(request, "supervision/annuaire_filtres.html", ctx)


@permission_required("users.acces_supervision")
def annuaire_export_tableau(request):
    ctx = {}
    ctx["id_selected"] = request.GET.getlist("filtres")

    from openpyxl import Workbook
    from tempfile import NamedTemporaryFile
    from outils.templatetags.outils_extras import tel_no_code

    dest_filename = "annuaire_cooperateurs_{}.xlsx".format(
        datetime.now().strftime("%Y%m%d%H%M")
    )

    wb = Workbook()
    ws = wb.active
    ws.title = "Annuaire"

    membres = SiteProduction.objects.filter(
        archive=False, entreprise_agricole__etat__pk__in=ctx["id_selected"]
    ).order_by("id_public")

    num_row = 1
    ws.append(
        [
            "N°",
            "Entreprise agricole",
            "Etat",
            "Adresse",
            "CP",
            "Ville",
            "Nb Salariés",
            "Nb Associés",
            "Nom",
            "Prénom",
            "Tel.",
            "Port.",
            "Email",
            "Date naissance",
            "SIRET",
            "TVA",
            "SAU",
            "Début campagne",
            "Volume contrat (en cours)",
        ]
    )
    num_row += 1
    for m in membres:
        associes = m.entreprise_agricole.get_personnel.order_by("ordre")
        nb_assoc = associes.count()
        cpt_assoc = 0
        for personnel in associes:
            ligne = [
                "",
                "",
                "",
                "",
                "",
                "",
                " ",
                "",
                "{}".format(personnel.personne.nom),
                "{}".format(personnel.personne.prenom),
                "{}".format(tel_no_code(personnel.personne.telephone)),
                "{}".format(tel_no_code(personnel.personne.portable)),
                "{}".format(personnel.personne.email)
                if personnel.personne.email
                else "",
                "{}".format(personnel.personne.date_naissance.strftime("%d/%m/%Y"))
                if personnel.personne.date_naissance
                else "",
                "",
                "",
                "",
                "",
                "",
            ]
            if cpt_assoc == 0:
                ligne[0] = m.id_public
                ligne[1] = m.entreprise_agricole.raison_sociale
                if m.entreprise_agricole.etat:
                    ligne[2] = m.entreprise_agricole.etat.label
                if m.entreprise_agricole.adresse:
                    if m.entreprise_agricole.adresse.adresse1:
                        ligne[3] = m.entreprise_agricole.adresse.adresse1
                    if m.entreprise_agricole.adresse.adresse2:
                        ligne[3] += " {}".format(m.entreprise_agricole.adresse.adresse2)
                    if m.entreprise_agricole.adresse.code_postal:
                        ligne[4] = m.entreprise_agricole.adresse.code_postal
                    if m.entreprise_agricole.adresse.ville:
                        ligne[5] = m.entreprise_agricole.adresse.ville
                if m.entreprise_agricole.nb_salaries != None:
                    ligne[6] = m.entreprise_agricole.nb_salaries
                if nb_assoc:
                    ligne[7] = nb_assoc
                if m.entreprise_agricole.siret:
                    ligne[14] = m.entreprise_agricole.siret
                if m.entreprise_agricole.n_tva:
                    ligne[15] = m.entreprise_agricole.n_tva
                ligne[16] = m.entreprise_agricole.sau
                demarrage = (
                    ContratProducteur.objects.filter(entreprise=m.entreprise_agricole)
                    .order_by("pk")
                    .last()
                )
                if demarrage:
                    ligne[17] = "{}/{}".format(
                        demarrage.date_debut_campagne.day,
                        demarrage.date_debut_campagne.month,
                    )
                contrat = (
                    ContratProducteur.objects.filter(
                        entreprise=m.entreprise_agricole,
                        date_debut__lte=datetime.now(),
                        date_fin__gte=datetime.now(),
                    )
                    .order_by("date_debut", "pk")
                    .last()
                )
                if contrat:
                    ligne[18] = "{:,}L".format(contrat.volume).replace(",", " ")

            ws.append(ligne)
            cpt_assoc += 1

    with NamedTemporaryFile() as tmp:
        wb.save(tmp.name)
        tmp.seek(0)
        response = HttpResponse(content=tmp.read(), content_type="application/ms-excel")
    response["Content-Disposition"] = "attachment; filename={}".format(dest_filename)
    return response


@permission_required("users.acces_supervision")
def cooperative_dashboard(request):
    ctx = {}

    return render(request, "supervision/cooperative-dashboard.html", ctx)


@permission_required("users.acces_supervision")
def gouvernance(request):
    ctx = OrderedDict()
    ctx["groupes"] = Groupe.objects.all()

    return render(request, "supervision/gouvernance.html", ctx)


@permission_required("users.acces_supervision")
def bilan_activite(request, params):
    ctx = {
        "params": params,
        "marges_json": json.dumps(get_marges(params), cls=DjangoJSONEncoder),
    }

    return render(request, "supervision/bilan-activite.html", ctx)


@permission_required("users.acces_supervision")
def bilan_activite_moteur(request):
    ctx = {}
    ctx["producteurs_json"] = json.dumps(liste_prods(), cls=DjangoJSONEncoder)
    ctx["clients_json"] = json.dumps(liste_clients(), cls=DjangoJSONEncoder)

    return render(request, "supervision/bilan-activite-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_perte_lait_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-perte-lait-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_perte_lait(request, params):
    ctx = {
        "params": params,
        "pertes_json": json.dumps(get_pertes_lait(params), cls=DjangoJSONEncoder),
    }
    return render(request, "supervision/bilan-perte-lait.html", ctx)


@permission_required("users.acces_supervision")
def bilan_collectes_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-collectes-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_collectes(request, params):
    bilan_collectes = get_bilan_collectes(params)
    dest_filename = "supervision_bilan-collectes_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_collectes(bilan_collectes, dest_filename)


@permission_required("users.acces_supervision")
def bilan_delestages_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-delestages-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_delestages(request, params):
    bilan_delestages = get_bilan_delestages(params)
    dest_filename = "supervision_bilan-delestages_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_delestages(bilan_delestages, dest_filename)


@permission_required("users.acces_supervision")
def bilan_previsions_collectes_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-previsions-collectes-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_previsions_collectes(request, params):
    bilan_previsions_collectes = get_bilan_previsions_collectes(params)
    dest_filename = "supervision_bilan-previsions-collectes_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_previsions_collectes(bilan_previsions_collectes, dest_filename)


@permission_required("users.acces_supervision")
def bilan_previsions_livraisons_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-previsions-livraisons-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_previsions_livraisons(request, params):
    bilan_previsions_livraisons = get_bilan_previsions_livraisons(params)
    dest_filename = "supervision_bilan-previsions-livraisonss_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_previsions_livraisons(
        bilan_previsions_livraisons, dest_filename
    )


@permission_required("users.acces_supervision")
def bilan_collectes_jours_moteur(request):
    ctx = {}
    return render(request, "supervision/bilan-collectes-jours-moteur.html", ctx)


@permission_required("users.acces_supervision")
def bilan_collectes_jours(request, params):
    bilan_collectes = get_bilan_collectes_jours(params)
    dest_filename = "supervision_bilan-collectes_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_collectes(bilan_collectes, dest_filename)


@permission_required("users.acces_supervision")
def prod_volumes_quotidiens_moteur(request):
    ctx = {}
    return render(request, "supervision/prod-volumes-quotidiens-moteur.html", ctx)


@permission_required("users.acces_supervision")
def prod_volumes_quotidiens(request, params):
    volumes = get_prod_volumes_quotidiens_tk(params)
    dest_filename = "supervision_prod-volumes-quotidiens_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_prod_volumes_quotidiens_tk(volumes, dest_filename)


@permission_required("users.acces_supervision")
def visites_moteur(request):
    ctx = {}
    ctx["filtres_json"] = json.dumps(get_filtres_visites(), cls=DjangoJSONEncoder)

    return render(request, "supervision/visites-moteur.html", ctx)


@permission_required("users.acces_supervision")
def visites(request, params):
    ctx = {
        "params": params,
        "bilan_json": json.dumps(get_bilan_visites(params), cls=DjangoJSONEncoder),
    }

    return render(request, "supervision/visites.html", ctx)


@permission_required("users.acces_supervision")
def prevision_activite_moteur(request):
    ctx = {}
    return render(request, "supervision/prevision-activite-moteur.html", ctx)


@permission_required("users.acces_supervision")
def prevision_activite(request, params):
    ctx = {
        "params": params,
        "previsions_json": json.dumps(get_previsions(params), cls=DjangoJSONEncoder),
    }
    return render(request, "supervision/prevision-activite.html", ctx)


@permission_required("users.acces_supervision")
def prevision_activite_export(request, params):
    previsions = get_previsions(params)
    dest_filename = "supervision_previsions_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_prevision_activite(previsions, dest_filename)


@permission_required("users.acces_supervision")
def bilan_activite_export(request, params):
    marges = get_marges(params)
    dest_filename = "supervision_bilan_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_bilan_activite(marges, dest_filename)


@permission_required("users.acces_supervision")
def stats_total_ht(request):
    ctx = {
        "debut": request.GET.get(
            "debut", date.today().replace(day=1).strftime("%Y-%m-%d")
        ),
        "fin": request.GET.get("fin", date.today().strftime("%Y-%m-%d")),
        "mois": OrderedDict(),
        "tot": {"ht": 0},
    }
    ventes = VenteLait.objects.filter(
        date__gte=ctx["debut"],
        date__lte=ctx["fin"],
    ).order_by("date")

    if ventes:
        for v in ventes:
            num_mois = "{}/{}".format(v.date.month, v.date.year)
            if not num_mois in ctx["mois"]:
                ctx["mois"][num_mois] = {"ht": 0}
            ctx["tot"]["ht"] += v.total_ht
            ctx["mois"][num_mois]["ht"] += v.total_ht

    return render(request, "supervision/stats-total-ht.html", ctx)


@permission_required("users.acces_supervision")
def transporteurs_releves_moteur(request):
    ctx = {}
    return render(request, "supervision/transporteurs-releves-moteur.html", ctx)


@permission_required("users.acces_supervision")
def transporteurs_releves(request, params):
    dest_filename = "supervision_transporteurs-releves_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_transporteurs_releves(request, dest_filename, params)


@permission_required("users.acces_supervision")
def analyses_producteurs_moteur(request):
    ctx = {}
    return render(request, "supervision/analyses-producteurs-moteur.html", ctx)


@permission_required("users.acces_supervision")
def analyses_producteurs(request, params):
    analyses = get_analyses_producteurs(params)
    dest_filename = "supervision_analyses-producteurs_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_analyses_producteurs(analyses, dest_filename)


@permission_required("users.acces_supervision")
def volumes_contractualises_clients_moteur(request):
    ctx = {}
    return render(
        request, "supervision/volumes-contractualises-clients-moteur.html", ctx
    )


@permission_required("users.acces_supervision")
def volumes_contractualises_clients(request, date_debut_filtre, date_fin_filtre):
    ctx = {}
    # ajustement dates du filtre
    date_debut = datetime.strptime(date_debut_filtre, "%Y-%m-%d")
    date_fin = datetime.strptime(date_fin_filtre, "%Y-%m-%d")
    if (
        date_fin > datetime.today()
    ):  # s'arreter a la date du jour, inutile calculer futur
        date_fin = datetime.today()

    ctx["list_client"] = get_dict_volumes_contractualises_clients(date_debut, date_fin)

    ctx["list_client"].sort(key=lambda item: item["totaux"]["litrage"], reverse=True)

    total_nb_livraison = 0
    total_lastyear_contrat = 0
    total_prevu_contrat = 0
    total_realise = 0
    total_moy_msu = 0
    total_ec = 0
    nb_clients_msu_null = 0
    for client in ctx["list_client"]:
        total_nb_livraison += client["totaux"]["nb_bl"]
        total_lastyear_contrat += client["totaux"]["litrage_lastyear_contrat"][0]
        total_prevu_contrat += client["totaux"]["litrage_prevu_contrat"][0]
        total_realise += client["totaux"]["litrage"]
        total_moy_msu += client["totaux"]["moy_msu"]
        if client["totaux"]["moy_msu"] == 0:
            nb_clients_msu_null += 1
        total_ec += client["totaux"]["ec"][0]

    total_moy_msu = int(total_moy_msu / (len(ctx["list_client"]) - nb_clients_msu_null))

    # difference reel vs contrat prevu total
    color_prevu_total = "black-text"
    if total_prevu_contrat != 0:
        diff_prevu_total = int(
            (total_prevu_contrat - total_realise) / (total_prevu_contrat) * 100
        )  # ecart en %
        if abs(diff_prevu_total) < 10:
            color_prevu_total = "green-text"
        elif 10 <= abs(diff_prevu_total) < 20:
            color_prevu_total = "orange-text"
        elif abs(diff_prevu_total) >= 20:
            color_prevu_total = "red-text"
    else:
        diff_prevu_total = 0
        color_prevu_total = "black-text"

    # difference reel vs contrat A-1 total
    color_lastyear_total = "black-text"
    if total_lastyear_contrat != 0:
        diff_lastyear_total = int(
            (total_lastyear_contrat - total_realise) / (total_lastyear_contrat) * 100
        )  # ecart en %
        if abs(diff_lastyear_total) < 10:
            color_lastyear_total = "green-text"
        elif 10 <= abs(diff_lastyear_total) < 20:
            color_lastyear_total = "orange-text"
        elif abs(diff_lastyear_total) >= 20:
            color_lastyear_total = "red-text"
    else:
        diff_lastyear_total = 0
        color_lastyear_total = "black-text"

    ctx["total_nb_livraison"] = total_nb_livraison
    ctx["total_lastyear_contrat"] = [
        total_lastyear_contrat,
        -diff_lastyear_total,
        color_lastyear_total,
    ]
    ctx["total_prevu_contrat"] = [
        total_prevu_contrat,
        -diff_prevu_total,
        color_prevu_total,
    ]
    ctx["total_realise"] = total_realise
    ctx["total_moy_msu"] = total_moy_msu
    ctx["total_ec"] = total_ec
    ctx["total_ec_litre"] = -round(total_ec / total_realise, 3)

    return render(request, "supervision/volumes-contractualises-clients.html", ctx)


@permission_required("users.acces_supervision")
def volumes_realises_contrats_moteur(request):
    ctx = {}
    return render(
        request, "supervision/volumes-realises-contrats-clients-moteur.html", ctx
    )


@permission_required("users.acces_supervision")
def volumes_realises_contrats_clients(request, date_debut_filtre, date_fin_filtre):
    ctx = {}
    # ajustement dates du filtre
    date_debut = datetime.strptime(date_debut_filtre, "%Y-%m-%d")
    date_fin = datetime.strptime(date_fin_filtre, "%Y-%m-%d")
    if (
        date_fin > datetime.today()
    ):  # s'arreter a la date du jour, inutile calculer futur
        date_fin = datetime.today()

    ctx["list_contrat"] = get_dict_volumes_contractualises_contrat(date_debut, date_fin)

    ctx["list_contrat"].sort(key=lambda item: item["totaux"]["litrage"], reverse=True)

    total_nb_livraison = 0
    total_lastyear_contrat = 0
    total_prevu_contrat = 0
    total_realise = 0
    total_moy_msu = 0
    total_ec = 0
    nb_clients_msu_null = 0
    for contrat in ctx["list_contrat"]:
        total_nb_livraison += contrat["totaux"]["nb_bl"]
        total_lastyear_contrat += contrat["totaux"]["litrage_lastyear_contrat"][0]
        total_prevu_contrat += contrat["totaux"]["litrage_prevu_contrat"][0]
        total_realise += contrat["totaux"]["litrage"]
        total_moy_msu += contrat["totaux"]["moy_msu"]
        if contrat["totaux"]["moy_msu"] == 0:
            nb_clients_msu_null += 1
        total_ec += contrat["totaux"]["ec"][0]

    total_moy_msu = int(
        total_moy_msu / (len(ctx["list_contrat"]) - nb_clients_msu_null)
    )

    # difference reel vs contrat prevu total
    color_prevu_total = "black-text"
    if total_prevu_contrat != 0:
        diff_prevu_total = int(
            (total_prevu_contrat - total_realise) / (total_prevu_contrat) * 100
        )  # ecart en %
        if abs(diff_prevu_total) < 10:
            color_prevu_total = "green-text"
        elif 10 <= abs(diff_prevu_total) < 20:
            color_prevu_total = "orange-text"
        elif abs(diff_prevu_total) >= 20:
            color_prevu_total = "red-text"
    else:
        diff_prevu_total = 0
        color_prevu_total = "black-text"

    # difference reel vs contrat A-1 total
    color_lastyear_total = "black-text"
    if total_lastyear_contrat != 0:
        diff_lastyear_total = int(
            (total_lastyear_contrat - total_realise) / (total_lastyear_contrat) * 100
        )  # ecart en %
        if abs(diff_lastyear_total) < 10:
            color_lastyear_total = "green-text"
        elif 10 <= abs(diff_lastyear_total) < 20:
            color_lastyear_total = "orange-text"
        elif abs(diff_lastyear_total) >= 20:
            color_lastyear_total = "red-text"
    else:
        diff_lastyear_total = 0
        color_lastyear_total = "black-text"

    ctx["total_nb_livraison"] = total_nb_livraison
    ctx["total_lastyear_contrat"] = [
        total_lastyear_contrat,
        -diff_lastyear_total,
        color_lastyear_total,
    ]
    ctx["total_prevu_contrat"] = [
        total_prevu_contrat,
        -diff_prevu_total,
        color_prevu_total,
    ]
    ctx["total_realise"] = total_realise
    ctx["total_moy_msu"] = total_moy_msu
    ctx["total_ec"] = total_ec
    ctx["total_ec_litre"] = -round(total_ec / total_realise, 3)

    return render(request, "supervision/volumes-realises-contrats-clients.html", ctx)


def entreprises_paturage(request, annee):
    ctx = {}
    ctx["actual_year"] = False
    ctx["annee"] = annee
    ctx["list_years"] = range(2020, int(date.today().year) + 1)
    # exclus anciens membres et ponctuels
    sites = list(
        SiteProduction.objects.exclude(entreprise_agricole__etat__id__in=[2, 4])
        .filter(archive=False)
        .order_by("id_public")
    )
    ctx["entreprises"] = []

    for site in sites:
        nb_jours_paturage = JourPaturage.objects.filter(
            lieu=site.lieu, date__year=annee
        ).count()
        ligne = {"site": site, "jours_paturage": nb_jours_paturage}
        ctx["entreprises"].append(ligne)

    return render(request, "supervision/paturages.html", ctx)


@permission_required("users.acces_supervision")
def prevision_test_differentielmsu(request, params):
    dest_filename = "test_differentiel-msu_{}_{}.xlsx".format(
        params, datetime.now().strftime("%m%d%H%M")
    )
    return export_test_differentielmsu(params, dest_filename)


@permission_required("users.acces_supervision")
def station_stockage_io_moteur(request):
    ctx = {}
    return render(request, "supervision/station-stockage-io-moteur.html", ctx)


@permission_required("users.acces_supervision")
def station_stockage_io(request, params):
    dest_filename = "supervision_station-stockage-io_{}_{}.xlsx".format(
        params, datetime.now().strftime("%Y%m%d%H%M")
    )
    return export_station_stockage_io(request, dest_filename, params)


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

