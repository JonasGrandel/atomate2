"""Module defining elastic document builder."""

from itertools import chain
from typing import List

import numpy as np
from maggma.builders import Builder
from maggma.core import Store
from pydash import get
from pymatgen.analysis.elasticity import Deformation, Stress

from atomate2.common.schemas.elastic import ElasticDocument
from atomate2.settings import settings


class ElasticBuilder(Builder):
    """
    The elastic builder compiles deformation tasks into an elastic document.

    The process can be summarised as:

    1. Find all deformation documents with the same formula.
    2. Group the deformations by their parent structures.
    3. Create an ElasticDocument from the group of tasks.

    Parameters
    ----------
    tasks
        Store of task documents.
    elasticity
        Store for final elastic documents.
    query
        Dictionary query to limit tasks to be analyzed.
    """

    def __init__(
        self,
        tasks: Store,
        elasticity: Store,
        query: dict = None,
        symprec: float = settings.SYMPREC,
        fitting_method: str = settings.ELASTIC_FITTING_METHOD,
        structure_match_tol: float = 1e-5,
        **kwargs,
    ):

        self.tasks = tasks
        self.elasticity = elasticity
        self.query = query if query else {}
        self.kwargs = kwargs
        self.symprec = symprec
        self.fitting_method = fitting_method
        self.structure_match_tol = structure_match_tol

        super().__init__(sources=[tasks], targets=[elasticity], **kwargs)

    def ensure_indexes(self):
        """Ensure indices on the tasks and elasticity collections."""
        self.tasks.ensure_index("output.formula_pretty")
        self.tasks.ensure_index("last_updated")
        self.elasticity.ensure_index("fitting_data.uuids.0")
        self.elasticity.ensure_index("last_updated")

    def get_items(self):
        """
        Get all items to process into elastic documents.

        Yields
        ------
        list[dict]
            A list of deformation tasks aggregated by formula and containing the
            required data to generate elasticity documents.
        """
        self.logger.info("Elastic builder started")
        self.logger.debug("Adding indices")
        self.ensure_indexes()

        q = dict(self.query)

        # query for deformations
        q.update(
            {
                "output.transformations.history.0.@class": "DeformationTransformation",
                "output.orig_inputs.NSW": {"$gt": 1},
                "output.orig_inputs.ISIF": {"$gt": 2},
            }
        )
        return_props = [
            "uuid",
            "output.transformations",
            "output.output.stress",
            "output.formula_pretty",
            "output.dir_name",
        ]

        self.logger.info("Starting aggregation")
        nformulas = len(self.tasks.distinct("output.formula_pretty", criteria=q))
        results = self.tasks.groupby(
            "output.formula_pretty", criteria=q, properties=return_props
        )
        self.logger.info("Aggregation complete")

        for n, (keys, docs) in enumerate(results):
            self.logger.debug(
                f"Getting {keys['output']['formula_pretty']} ({n + 1} of {nformulas})"
            )
            yield docs

    def process_item(self, tasks: List[dict]) -> List[ElasticDocument]:
        """
        Process deformation tasks into elasticity documents.

        The deformation tasks will be grouped based on their parent structure (i.e., the
        structure before the deformation was applied).

        Parameters
        ----------
        tasks
            A list of deformation task, all with the same formula.

        Returns
        -------
        list[ElasticDocument]
            A list of elastic documents for each unique parent structure.
        """
        self.logger.debug(f"Processing {tasks[0]['output']['formula_pretty']}")

        if not tasks:
            return []

        # group deformations by parent structure
        grouped = group_deformations(tasks, self.structure_match_tol)

        elastic_docs = []
        for tasks in grouped:
            elastic_doc = get_elastic_document(tasks, self.symprec, self.fitting_method)

            if elastic_doc is not None:
                elastic_docs.append(elastic_doc)

        return elastic_docs

    def update_targets(self, items: List[ElasticDocument]):
        """
        Insert new elastic documents into the elasticity store.

        Parameters
        ----------
        items
            A list of elasticity documents.
        """
        items = chain.from_iterable(filter(bool, items))  # type: ignore

        if len(items) > 0:
            self.logger.info(f"Updating {len(items)} elastic documents")
            self.elasticity.update(items, key="fitting_data.uuids.0")
        else:
            self.logger.info("No items to update")


def group_deformations(tasks: List[dict], tol: float) -> List[List[dict]]:
    """
    Group deformation tasks by their parent structure.

    Parameters
    ----------
    tasks
        A list of deformation tasks.
    tol
        Numerical tolerance for structure equivalence.

    Returns
    -------
    list[list[dict]]
        The tasks grouped by their parent (undeformed structure).
    """
    grouped_tasks = [[tasks[0]]]

    for task in tasks[1:]:
        orig_structure = get(task, "output.transformations.history.0.input_structure")

        for group in grouped_tasks:
            group_orig_structure = get(
                group[0], "output.transformations.history.0.input_structure"
            )

            # strict but fast structure matching, the structures should be identical
            lattice_match = np.allclose(
                orig_structure.lattice.matrix,
                group_orig_structure.lattice.matrix,
                atol=tol,
            )
            coords_match = np.allclose(
                orig_structure.frac_coords, group_orig_structure.frac_coords, atol=tol
            )
            if lattice_match and coords_match:
                group.append(task)
                break

    return grouped_tasks


def get_elastic_document(
    tasks: List[dict],
    symprec: float,
    fitting_method: str,
) -> ElasticDocument:
    """
    Turn a list of deformation tasks into an elastic document.

    Parameters
    ----------
    tasks
        A list of deformation tasks.
    symprec
        Symmetry precision for deriving symmetry equivalent deformations. If
        ``symprec=None``, then no symmetry operations will be applied.
    fitting_method
        The method used to fit the elastic tensor. See pymatgen for more details on the
        methods themselves. The options are:
        - "finite_difference" (note this is required if fitting a 3rd order tensor)
        - "independent"
        - "pseudoinverse"

    Returns
    -------
    ElasticDocument
        An elastic document.
    """
    structure = get(tasks[0], "output.transformations.history.0.input_structure")

    stresses = []
    deformations = []
    uuids = []
    job_dirs = []
    for doc in tasks:
        deformations.append(
            Deformation(get(doc, "output.transformations.history.0.deformation"))
        )
        stresses.append(Stress(get(doc, "output.output.stress")))
        uuids.append(doc["uuid"])
        job_dirs.append(doc["output"]["dir_name"])

    return ElasticDocument.from_stresses(
        structure,
        stresses,
        deformations,
        uuids,
        job_dirs,
        fitting_method=fitting_method,
        symprec=symprec,
    )
