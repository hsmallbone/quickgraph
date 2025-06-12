"""Dashboard router."""

import logging
from collections import defaultdict
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from ..dataset.schemas import QualityFilter, SaveStateFilter
from ..dependencies import get_active_project_user, get_db
from ..project.schemas import FlagState, OntologyItem
from ..resources.services import get_project_ontology_items
from ..users.schemas import UserDocumentModel
from ..utils.agreement import AgreementCalculator
from ..utils.misc import flatten_hierarchical_ontology
from ..utils.services import create_search_regex
from .schemas import AdjudicationResponse, DashboardInformation
from .services import (
    calculate_project_progress,
    create_overview_plot_data,
    filter_annotations,
    get_dashboard_information,
    group_data_by_key,
)
from ..settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix=f"{settings.api.prefix}/dashboard", tags=["Dashboard"])


@router.get("/{project_id}", response_model=DashboardInformation)
async def get_dashboard_info_endpoint(
    project_id: str,
    user: UserDocumentModel = Depends(get_active_project_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Fetches a project dashboard information."""
    result = await get_dashboard_information(
        db=db, project_id=ObjectId(project_id), username=user.username
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )
    return result


@router.get("/overview/{project_id}")
async def get_overview(
    project_id: str,
    user: UserDocumentModel = Depends(get_active_project_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """
    Fetches project dashboard overview.

    This function aggregates project data and returns it as high-level measures of progress and data for visualisation.
    Overview measures of progress include:
        - "project progress": This is the progress made to date. It is calculated as: (saved dataset items with minium annotators) / (total dataset items)
        - "overall agreement": This is the mean entity/relation agreement for relation projects, otherwise it is equivalent to 'average entity agreement' for entity only projects.
        - "average entity agreement": This is the mean entity agreement.
        - "average relation agreement": This is the mean relation agreement.
        - "entities created": This is the number of entities with majority annotator agreement.
        - "triples created": This is the number of triplets with majority annotator agreement.

    Visualisations include:
        - "project progress": This is a temporal distribution of save states applied by project annotators.
        - "entities": This is the distribution of applied entities by project annotators - both silver and weak.
        - "relations" This is the distribution of applied relations by project annotators - both silver and weak.
        - "triples": This is the top-n most frequent triplet structures applied by project annotators - both silver and weak.
        - "flags": This is the distribution of flags applied by project annotators.
        - "social": This is the distribution of social interaction across the project dataset items.

    Parameters
    ----------
    project_id : str
        The UUID of the project.

    """

    # Convert "project_id" in bson object
    project_id = ObjectId(project_id)

    # Fetch project details
    project = await db.projects.find_one({"_id": project_id})
    is_relation_project = project["tasks"]["relation"]

    # Create overview plot data
    plot_data = await create_overview_plot_data(
        db=db,
        project_id=project_id,
        is_relation_project=is_relation_project,
    )

    # Calculate overview metrics
    project_progress_metrics = await calculate_project_progress(project_id, db)

    dataset_item_pipeline = [
        {"$match": {"project_id": project_id}},
        {
            "$project": {
                "_id": 1,
                "save_count": {"$size": {"$ifNull": ["$save_states", []]}},
            }
        },
        {
            "$match": {
                "save_count": {"$gte": project["settings"]["annotators_per_item"]}
            }
        },
    ]

    dataset_items = await db["data"].aggregate(dataset_item_pipeline).to_list(None)
    dataset_item_ids = [di["_id"] for di in dataset_items]
    logger.info(f"calculating agreement on {len(dataset_items)} dataset items")

    # Get agreement metrics for accepted entities on dataset items that have been saved by majority
    entity_markup = (
        await db["markup"]
        .find(
            {
                "project_id": project_id,
                "classification": "entity",
                "suggested": False,
                "dataset_item_id": {"$in": dataset_item_ids},
            }
        )
        .to_list(None)
    )

    # Relation is accepted only on dataset items that have been saved by majority
    relation_markup = []
    if is_relation_project:
        pipeline = [
            {
                "$match": {
                    "project_id": project_id,
                    "classification": "relation",
                    "suggested": False,
                    "dataset_item_id": {"$in": dataset_item_ids},
                }
            },
            {
                "$lookup": {
                    "from": "markup",
                    "localField": "source_id",
                    "foreignField": "_id",
                    "as": "source",
                }
            },
            {
                "$lookup": {
                    "from": "markup",
                    "localField": "target_id",
                    "foreignField": "_id",
                    "as": "target",
                }
            },
            {"$unwind": {"path": "$source"}},
            {"$unwind": {"path": "$target"}},
            {
                "$project": {
                    "source.project_id": 0,
                    "source.dataset_item_id": 0,
                    "source.created_at": 0,
                    "source.updated_at": 0,
                    "source.created_by": 0,
                    "target.project_id": 0,
                    "target.dataset_item_id": 0,
                    "target.created_at": 0,
                    "target.updated_at": 0,
                    "target.created_by": 0,
                }
            },
        ]

        relation_markup = await db["markup"].aggregate(pipeline).to_list(None)

    agreement_calculator = AgreementCalculator(
        entity_data=[
            {
                "start": m["start"],
                "end": m["end"],
                "label": m["ontology_item_id"],
                "username": m["created_by"],
                "doc_id": str(m["dataset_item_id"]),
            }
            for m in entity_markup
        ],
        relation_data=[
            {
                "label": m["ontology_item_id"],
                "username": m["created_by"],
                "source": {
                    "start": m["source"]["start"],
                    "end": m["source"]["end"],
                    "label": m["source"]["ontology_item_id"],
                },
                "target": {
                    "start": m["target"]["start"],
                    "end": m["target"]["end"],
                    "label": m["target"]["ontology_item_id"],
                },
                "doc_id": str(m["dataset_item_id"]),
            }
            for m in relation_markup
        ],
    )

    entity_overall_agreement_score = agreement_calculator.overall_agreement()

    relation_overall_agreement_score = agreement_calculator.overall_agreement(
        "relation"
    )

    overall_agreement_score = agreement_calculator.overall_average_agreement()

    agreed_entity_count = agreement_calculator.count_majority_agreements()

    agreed_relation_count = agreement_calculator.count_majority_agreements("relation")

    output = {
        "metrics": [
            {
                "index": 0,
                "name": "Project Progress",
                "title": "Progress made to date (only counts documents saved by the minimum number of annotators)",
                "value": f"{project_progress_metrics['percentage']:0.0f}%",
            },
            {
                "index": 2,
                "name": "Average Entity Agreement",
                "title": "Average entity inter-annotator agreement",
                "value": (
                    None
                    if entity_overall_agreement_score is None
                    else f"{entity_overall_agreement_score*100:0.0f}%"
                ),
            },
            {
                "index": 4,
                "name": "Entities Created",
                "title": "Count of agreed upon entities (silver and weak) created by annotators",
                "value": agreed_entity_count,
            },
        ],
        "plots": plot_data,
    }

    if project["tasks"]["relation"]:
        output["metrics"] += [
            {
                "index": 3,
                "name": "Average Relation Agreement",
                "title": "Average relation inter-annotator agreement",
                "value": (
                    None
                    if relation_overall_agreement_score is None
                    else f"{relation_overall_agreement_score*100:0.0f}%"
                ),
            },
            {
                "index": 5,
                "name": "Triples Created",
                "title": "Count of agreed upon triples (silver and weak) created by annotators",
                "value": agreed_relation_count,
            },
            {
                "index": 1,
                "name": "Overall Agreement",
                "title": "Weighted average overall inter-annotator agreement",
                "value": (
                    None
                    if (overall_agreement_score is None)
                    else f"{overall_agreement_score*100:0.0f}%"
                ),
            },
        ]

    return output


@router.get("/adjudication/{project_id}")
async def get_adjudication_endpoint(
    project_id: str,
    skip: int = Query(default=0, min=0),
    search_term: Optional[str] = Query(
        default=None,
        title="Search Term",
        description="Search term to filter dataset items on.",
    ),
    flags: Optional[str] = Query(
        default=None,
        title="Flags",
        description="Stringified comma separated list of flags to filter dataset items on.",
    ),
    sort: int = Query(
        default=-1,
        ge=-1,
        le=1,
        description="Overall inter-annotator agreement sorting: ascending - high to low (-1), descending - low to high (1), no sorting (0).",
    ),
    min_agreement: int = Query(
        default=0,
        ge=0,
        le=100,
        description="The minimum overall inter-annotator agreement of documents to return.",
    ),
    dataset_item_id: Optional[str] = Query(
        default=None,
        title="Dataset Item Id",
        description="Dataset item id to filter dataset items on.",
    ),
    user: UserDocumentModel = Depends(get_active_project_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Gets adjudication information for a single dataset item.

    Only returns items that have greater than 0 agreement.

    TODO
    ----
    - Return metrics: most_common_markup, most_agreed_markup, most_disagreed_markup,
    average_annotations, user_most_annotations, user_least_annotations,
    user_highest_agreement, and user_lowest_agreement
    """

    project_id = ObjectId(project_id)

    match_condition = {
        "$match": {
            "project_id": project_id,
            "iaa.agreement.overall": {"$gt": min_agreement / 100},
        }
    }

    if dataset_item_id:
        logger.info('Filtering adjudication on "dataset_item_id')
        match_condition["$match"] = {
            **match_condition["$match"],
            "_id": ObjectId(dataset_item_id),
        }

    if search_term:
        logger.info('Filtering adjudication on "search_term"')
        search_term_regex = create_search_regex(
            search_term
        )  # re.compile(rf"\b{re.escape(search_term)}\b", re.IGNORECASE)
        match_condition["$match"] = {
            **match_condition["$match"],
            "text": {"$regex": search_term_regex},
        }

    if flags:
        logger.info(f"Flags :: {flags}")
        # Sanitize flags and ensure they are expected
        flags = [
            f
            for f in (f.strip() for f in flags.split(","))
            if f in list(set(FlagState)) + ["everything", "no_flags"]
        ]
        logger.info(f"Filters for flags :: {flags}")

        if "everything" in flags:
            pass
        elif "no_flags" in flags:
            match_condition["$match"] = {
                **match_condition["$match"],
                "$or": [{"flags": {"$exists": False}}, {"flags": {"$size": 0}}],
            }
        else:
            match_condition["$match"] = {
                **match_condition["$match"],
                "flags": {"$elemMatch": {"state": {"$in": flags}}},
            }

    logger.info(f"match condition: {match_condition}")

    # Get project dataset id
    project = await db.projects.find_one(
        {"_id": project_id},
        {"dataset_id": 1, "annotators": 1, "tasks": 1, "ontology": 1},
    )
    logger.info("Loaded project...")

    dataset_item_pipeline = [
        match_condition,
        {
            "$sort": {
                "iaa.overall": sort,
                "_id": 1,
            }  # Use "_id" to tie break otherwise same documents may be returned.
        },
        {"$skip": skip},
        {"$limit": 1},
    ]

    # Get dataset item (only one is returned at a time)
    dataset_item = await db.data.aggregate(dataset_item_pipeline).to_list(None)

    if len(dataset_item) == 0:
        # No dataset items found
        logger.info("No dataset items found")
        return {
            "save_states": [],
            "agreement": (
                {
                    "overall": None,
                    "relation": None,
                    "entity": None,
                }
                if project["tasks"]["relation"]
                else {
                    "overall": None,
                    "entity": None,
                }
            ),
            "tokens": None,
            "original": None,
            "total_items": 0,
            "_id": None,
            "updated_at": None,
            "annotators": [],
            "flags": [],
            "social": [],
            "entities": {},
            **({"relations": {}} if project["tasks"]["relation"] else {}),
        }

    dataset_item = dataset_item[0]

    # Get dataset item socials
    social = (
        await db["social"].find({"dataset_item_id": dataset_item["_id"]}).to_list(None)
    )
    social = [
        {
            "text": s["text"],
            "created_by": s["created_by"],
            "updated_at": s["updated_at"],
            "created_at": s["created_at"],
        }
        for s in social
    ]

    # Get dataset item markup
    markup = (
        await db["markup"]
        .find(
            {
                "dataset_item_id": dataset_item["_id"],
                "classification": {"$in": ["entity", "relation"]},
            }
        )
        .to_list(None)
    )
    # Get ontologies to get further information about the mark up
    _, _, ontology = await get_project_ontology_items(db=db, project_id=project_id)
    logger.info(f"ontology: {ontology}")
    ontologyId2Details = {item.id: item for item in ontology}
    entity_markup = []
    relation_markup = []

    logger.info(f"ontologyId2Details: {ontologyId2Details}")

    for m in markup:
        if m["classification"] == "entity":
            entity_markup.append(
                {
                    **m,
                    "ontology_item_name": ontologyId2Details[
                        m["ontology_item_id"]
                    ].name,
                    "ontology_item_fullname": ontologyId2Details[
                        m["ontology_item_id"]
                    ].fullname,
                    "ontology_item_color": ontologyId2Details[
                        m["ontology_item_id"]
                    ].color,
                }
            )
        else:
            relation_markup.append(
                {
                    "source_id": m["source_id"],
                    "target_id": m["target_id"],
                    "created_by": m["created_by"],
                    "ontology_item_id": m["ontology_item_id"],
                }
            )

    logger.info(f"entity_markup: {entity_markup}")

    # Get count of dataset items
    total_dataset_items = await db.data.count_documents(match_condition["$match"])

    # Find the last updated markup and convert to string for serialization
    last_updated = (
        max([m["updated_at"] for m in markup])
        if markup and len([m for m in markup if bool(m)]) > 0
        else None
    )

    return AdjudicationResponse(
        _id=str(dataset_item["_id"]),
        save_states=dataset_item["save_states"],
        agreement=dataset_item["iaa"]["agreement"],
        pairwise_agreement=dataset_item["iaa"]["pairwise_agreement"],
        tokens=dataset_item["tokens"],
        original=dataset_item["original"],
        total_items=total_dataset_items,
        updated_at=last_updated,
        annotators=[
            a["username"] for a in project["annotators"] if a["state"] == "accepted"
        ],
        flags=dataset_item["flags"],
        social=social,
        entities={
            **group_data_by_key(data=entity_markup, key="created_by"),
        },
        relations=(
            {**group_data_by_key(data=relation_markup, key="created_by")}
            if project["tasks"]["relation"]
            else None
        ),
    )


@router.get("/effort/{project_id}")
async def get_effort(
    project_id: str,
    saved: int = Query(default=SaveStateFilter.everything),
    quality: int = Query(default=QualityFilter.everything),
    min_agreement: int = Query(default=0, ge=0, le=100),
    user: UserDocumentModel = Depends(get_active_project_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Fetches a summary of annotation effort on a given project. Effort is used summarise the progress made by each annotator.

    TODO:
        - Implement min_agreement
        - Aggregated annotations ("gold standard")
    """

    return await filter_annotations(
        db=db,
        project_id=ObjectId(project_id),
        saved=saved,
        quality=quality,
        min_agreement=min_agreement,
        download_format=False,
    )


@router.get("/download/{project_id}")
async def get_download_endpoint(
    project_id: str,
    # saved: int = Query(default=SaveStateFilter.everything),
    # quality: int = Query(default=QualityFilter.everything),
    flags: list = Query(default=None),
    usernames: str = Query(default=None),
    # min_agreement: int = Query(default=0, ge=0, le=100),
    current_user: UserDocumentModel = Depends(get_active_project_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Fetches annotations for download

    Usernames are sent a comma separated string

    TODO:
        -
        - implement gold standard
        - implement min_agreement query param
    """
    if usernames is None:
        usernames = [current_user.username]
    else:
        usernames = usernames.split(",")

    project_id = ObjectId(project_id)

    logger.info(f"downloading for users: {usernames} on project {project_id}")

    # Convert ontology_item_ids into their fullnames for human readability
    project = await db["projects"].find_one(
        {"_id": project_id},
        {"tasks": 1, "entity_ontology_id": 1, "relation_ontology_id": 1},
    )

    if project["tasks"]["entity"]:
        # Get entity id from project
        entity_ontology_id = project["entity_ontology_id"]
        # Fetch entity ontology from resources collection
        entity_ontology = await db.resources.find_one({"_id": entity_ontology_id})
    if project["tasks"]["relation"]:
        # Get relation id from project
        relation_ontology_id = project["relation_ontology_id"]
        # Fetch relation ontology from resources collection
        relation_ontology = await db.resources.find_one({"_id": relation_ontology_id})

    # Combine entity and relation ontology 'content' items
    ontology = []
    if project["tasks"]["entity"]:
        for item in entity_ontology["content"]:
            ontology.append(OntologyItem(**item))
    if project["tasks"]["relation"]:
        for item in relation_ontology["content"]:
            ontology.append(OntologyItem(**item))

    flat_ontology = flatten_hierarchical_ontology(ontology=ontology)
    ontology_id2fullname = {item.id: item.fullname for item in flat_ontology}

    dataset_items = await db.data.find(
        {"project_id": project_id},
        {
            "tokens": 1,
            "original": 1,
            "text": 1,
            "extra_fields": 1,
            "external_id": 1,
            "save_states": 1,
            "flags": 1,
        },
    ).to_list(None)
    logger.info(f"loaded {len(dataset_items)} dataset items")

    markup = await db["markup"].find({"project_id": project_id}).to_list(None)
    logger.info(f"Loaded {len(markup)} markup")

    _map = defaultdict(list)
    for m in markup:
        _map[(str(m["dataset_item_id"]), m["created_by"])].append(m)

    entity_markup = defaultdict(list)
    relation_markup = defaultdict(list)
    for m in markup:
        di_id = str(m["dataset_item_id"])
        if m["classification"] == "entity":
            entity_markup[di_id].append(m)
        else:
            relation_markup[di_id].append(m)

    output = defaultdict(dict)
    for di in dataset_items:
        di_id = str(di["_id"])

        _entity_markup = [
            {
                "id": str(m["_id"]),
                "start": m["start"],
                "end": m["end"],
                "label": ontology_id2fullname.get(m["ontology_item_id"]),
                "annotator": m["created_by"],
            }
            for m in entity_markup[di_id]
        ]

        output[di_id] = {
            "id": di_id,
            "original": di["original"],
            "text": di["text"],
            "tokens": di["tokens"],
            "extra_fields": di["extra_fields"],
            "external_id": di["external_id"],
            # "saved": saved_by_creator,
            "entities": _entity_markup,
            # "relations": relation_markup[di],
            # "flags": user_flags,
        }

    #return output

    output = defaultdict(lambda: defaultdict(dict))
    # for m in markup:
    try:
        for di in dataset_items:
            for username in usernames:
                di_id = str(di["_id"])
                _markup = _map.get((di_id, username), [])

                entity_markup = [
                    {
                        "id": str(m["_id"]),
                        "start": m["start"],
                        "end": m["end"],
                        "label": ontology_id2fullname.get(m["ontology_item_id"]),
                        "annotator": username,
                    }
                    for m in _markup
                    if m["classification"] == "entity"
                ]

                entityId2Index = {
                    str(e["id"]): idx for idx, e in enumerate(entity_markup)
                }

                relation_markup = [
                    {
                        "id": str(m["_id"]),
                        "source_id": str(m["source_id"]),
                        "target_id": str(m["target_id"]),
                        "head": entityId2Index[str(m["source_id"])],
                        "tail": entityId2Index[str(m["target_id"])],
                        "label": ontology_id2fullname.get(m["ontology_item_id"]),
                        "annotator": username,
                    }
                    for m in _markup
                    if m["classification"] == "relation"
                ]

                # classification = m["classification"]
                # is_entity = classification == "entity"
                if di_id not in output[username].keys():
                    di_save_states = di.get("save_states", None)

                    saved_by_creator = (
                        (
                            len(
                                [
                                    ss
                                    for ss in di_save_states
                                    if ss["created_by"] == username
                                ]
                            )
                            == 1
                        )
                        if di_save_states
                        else False
                    )

                    flags = di.get("flags")
                    if flags is not None and isinstance(flags, list):
                        user_flags = [
                            f["state"] for f in flags if f.get("created_by") == username
                        ]
                    else:
                        user_flags = []

                    output[username][di_id] = {
                        "id": di_id,
                        "original": di["original"],
                        "text": di["text"],
                        "tokens": di["tokens"],
                        "extra_fields": di["extra_fields"],
                        "external_id": di["external_id"],
                        "saved": saved_by_creator,
                        "entities": entity_markup,
                        "relations": relation_markup,
                        "flags": user_flags,
                    }
    

    except Exception as e:
        logger.info(f"Exception: {e}")

    # Convert output into {"username": list(dataset_item with markup)}
    return {
        username: [
            {"id": di_id, **contents} for di_id, contents in dataset_items.items()
        ]
        for username, dataset_items in output.items()
    }

    # return await dashboard_services.filter_annotations(
    #     db=db,
    #     project_id=ObjectId(project_id),
    #     saved=saved,
    #     quality=quality,
    #     min_agreement=min_agreement,
    #     download_format=True,
    # )
