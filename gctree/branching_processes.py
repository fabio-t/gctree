r"""
This module contains classes for simulation and inference for a binary
branching process with mutation in which the tree is collapsed to nodes that
count the number of clonal leaves of each type.
"""

from __future__ import annotations

import gctree.utils
from gctree.isotyping import _isotype_dagfuncs
from gctree.mutation_model import _mutability_dagfuncs
from gctree.phylip_parse import disambiguate

import numpy as np
import warnings
import random
import os
import scipy.special as scs
import scipy.optimize as sco
import ete3
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from Bio import AlignIO
from Bio.Phylo.TreeConstruction import MultipleSeqAlignment
import pickle
import functools
import collections as coll
import historydag as hdag
import multiset
import matplotlib as mp
import matplotlib.pyplot as plt
from typing import Tuple, Dict, List, Union, Set, Callable, Mapping, Sequence

np.seterr(all="raise")


class CollapsedTree:
    r"""A collapsed tree, modeled as an infinite type Galton-Watson process run
    to extinction.

    Attributes:
        tree: :class:`ete3.TreeNode` object with ``abundance`` node features

    Args:
        tree: ete3 tree with ``abundance`` node features. Nonzero abundances expected on leaves, and optionally nodes adjacent to leaves via a path of length zero edges. If uncollapsed, it will be collapsed along branches with no mutations. Can be ommitted on initializaion, and later simulated.
        allow_repeats: tolerate the existence of nodes with the same genotype after collapse, e.g. in sister clades.
    """

    _max_ll_cache: Dict[Tuple[float, float], Tuple[int, int]] = {}

    def __init__(self, tree: ete3.TreeNode = None, allow_repeats: bool = False):
        if tree is not None:
            self.tree = tree.copy()
            self.tree.dist = 0

            # remove unobserved internal unifurcations
            for node in self.tree.iter_descendants():
                if node.abundance == 0 and len(node.children) == 1:
                    node.delete(prevent_nondicotomic=False)

            # ensure distances are correct before collapse
            for node in self.tree.iter_descendants():
                node.dist = gctree.utils.hamming_distance(
                    node.sequence, node.up.sequence
                )

            # iterate over the tree below root and collapse edges of zero
            # length if the node is a leaf and it's parent has nonzero
            # abundance we combine taxa names to a set to acommodate
            # bootstrap samples that result in repeated genotypes
            observed_genotypes = set((leaf.name for leaf in self.tree))
            observed_genotypes.add(self.tree.name)
            for node in self.tree.get_descendants(strategy="postorder"):
                if node.dist == 0:
                    node.up.abundance = max(node.abundance, node.up.abundance)
                    if isinstance(node.name, str):
                        node_set = set([node.name])
                    else:
                        node_set = set(node.name)
                    if isinstance(node.up.name, str):
                        node_up_set = set([node.up.name])
                    else:
                        node_up_set = set(node.up.name)
                    if node_up_set < observed_genotypes:
                        if node_set < observed_genotypes:
                            node.up.name = tuple(node_set | node_up_set)
                            if len(node.up.name) == 1:
                                node.up.name = node.up.name[0]
                    elif node_set < observed_genotypes:
                        node.up.name = tuple(node_set)
                        if len(node.up.name) == 1:
                            node.up.name = node.up.name[0]
                    node.delete(prevent_nondicotomic=False)

            final_observed_genotypes = set()
            for node in self.tree.traverse():
                if node.abundance > 0 or node == self.tree:
                    for name in (
                        (node.name,) if isinstance(node.name, str) else node.name
                    ):
                        final_observed_genotypes.add(name)
            if final_observed_genotypes != observed_genotypes:
                raise RuntimeError(
                    "observed genotypes don't match after "
                    f"collapse\n\tbefore: {observed_genotypes}"
                    f"\n\tafter: {final_observed_genotypes}\n\t"
                    "symmetric diff: "
                    f"{observed_genotypes ^ final_observed_genotypes}"
                )

            rep_seq = sum(node.abundance > 0 for node in self.tree.traverse()) - len(
                set(
                    [
                        node.sequence
                        for node in self.tree.traverse()
                        if node.abundance > 0
                    ]
                )
            )
            if not allow_repeats and rep_seq:
                raise RuntimeError(
                    "Repeated observed sequences in collapsed "
                    f"tree. {rep_seq} sequences were found repeated."
                )
            elif allow_repeats and rep_seq:
                rep_seq = sum(
                    node.abundance > 0 for node in self.tree.traverse()
                ) - len(
                    set(
                        [
                            node.sequence
                            for node in self.tree.traverse()
                            if node.abundance > 0
                        ]
                    )
                )
                print(
                    "Repeated observed sequences in collapsed tree. "
                    f"{rep_seq} sequences were found repeated."
                )
            # a custom ladderize accounting for abundance and sequence to break
            # ties in abundance
            for node in self.tree.traverse(strategy="postorder"):
                # add a partition feature and compute it recursively up tree
                node.add_feature(
                    "partition",
                    node.abundance + sum(node2.partition for node2 in node.children),
                )
                # sort children of this node based on partion and sequence
                node.children.sort(key=lambda node: (node.partition, node.sequence))

            self._build_cm_counts()
        else:
            self.tree = None

    def _build_cm_counts(self):
        # create tuple (c, m) for each node, and store in a tuple of
        # ((c, m), n)'s, where n is the multiplicity of (c, m) seen in the
        # tree, adding pseudocount to root if unobserved unifurcation at root.
        cmlist = [
            (node.abundance, len(node.children))
            for node in self.tree.iter_descendants()
        ]
        rootcm = (self.tree.abundance, len(self.tree.children))
        if rootcm == (0, 1):
            cmlist.append((1, 1))
        else:
            cmlist.append(rootcm)
        self._cm_counts = tuple(coll.Counter(cmlist).items())

    @staticmethod
    def _simulate_genotype(p: np.float64, q: np.float64) -> Tuple[int, int]:
        r"""Simulate the number of clonal leaves :math:`c` and mutant clades
        :math:`m` as a Galton-Watson process with branching probability
        :math:`p` and mutation probability :math:`q`.

        Args:
            p: branching probability
            q: mutation probability

        Returns:
            Tuple :math:`(c, m)` of the number of clonal leaves and mutant clades
        """
        if not (0 <= p <= 1 and 0 <= q <= 1):
            raise ValueError(
                "p and q must be in the unit interval: " f"p = {p}, q = {q}"
            )
        if p >= 0.5:
            warnings.warn(
                f"p = {p} is not subcritical, tree simulations not garanteed to terminate!"
            )
        # let's track the tree in breadth first order, listing number of clonal
        # and mutant descendants of each node mutant clades terminate in this
        # view
        cumsum_clones = 0
        len_tree = 0
        c = 0
        m = 0
        # while termination condition not met
        while cumsum_clones > len_tree - 1:
            if random.random() < p:
                mutants = sum(random.random() < q for child in range(2))
                clones = 2 - mutants
                m += mutants
            else:
                mutants = 0
                clones = 0
                c += 1
            cumsum_clones += clones
            len_tree += 1
        assert cumsum_clones == len_tree - 1
        return c, m

    @staticmethod
    @functools.lru_cache(maxsize=None)
    def _ll_genotype(
        c: int, m: int, p: np.float64, q: np.float64
    ) -> Tuple[np.float64, np.ndarray]:
        r"""Log-probability of getting :math:`c` leaves that are clones of the root and
        :math:`m` mutant clades off the root lineage, given branching probability :math:`p` and
        mutation probability :math:`q`.

        AKA the spaceship distribution. Also returns gradient wrt p and q
        (p, q). Computed by dynamic programming.

        Args:
            c: clonal leaves
            m: mutant clades
            p: branching probability
            q: mutation probability

        Returns:
            log-likelihood and gradient wrt :math:`p` and :math:`q`.
        """
        if (p, q) in CollapsedTree._max_ll_cache:
            cached_c, cached_m = CollapsedTree._max_ll_cache[(p, q)]
        else:
            cached_c, cached_m = 0, 0
        # Check cache is built
        if c > cached_c or m > cached_m:
            CollapsedTree._max_ll_cache[(p, q)] = (c, m)
            # Build missing cache in three parts:
            # |1 3
            # |X 2
            # Where 'X' is already built, axes are c and m.
            for cx in range(cached_c + 1):
                for mx in range(cached_m, m + 1):
                    if cx > 0 or mx > 1:
                        CollapsedTree._ll_genotype(cx, mx, p, q)
            for mx in range(cached_m + 1):
                for cx in range(cached_c, c + 1):
                    if cx > 0 or mx > 1:
                        CollapsedTree._ll_genotype(cx, mx, p, q)
            for mx in range(cached_m + 1, m + 1):
                for cx in range(cached_c + 1, c + 1):
                    if cx > 0 or mx > 1:
                        CollapsedTree._ll_genotype(cx, mx, p, q)
            # If we're here, we've computed what we want
            return CollapsedTree._ll_genotype(c, m, p, q)

        if c == m == 0 or (c == 0 and m == 1):
            raise ValueError("Zero likelihood event")
        elif c == 1 and m == 0:
            logf_result = np.log(1 - p)
            dlogfdp_result = -1 / (1 - p)
            dlogfdq_result = 0
        elif c == 0 and m == 2:
            logf_result = np.log(p) + 2 * np.log(q)
            dlogfdp_result = 1 / p
            dlogfdq_result = 2 / q
        else:
            if m >= 1:
                (
                    neighbor_ll_genotype,
                    (
                        neighbor_dlogfdp,
                        neighbor_dlogfdq,
                    ),
                ) = CollapsedTree._ll_genotype(c, m - 1, p, q)
                logg_array = [
                    (
                        np.log(2)
                        + np.log(p)
                        + np.log(q)
                        + np.log(1 - q)
                        + neighbor_ll_genotype
                    )
                ]
                dloggdp_array = [1 / p + neighbor_dlogfdp]
                dloggdq_array = [1 / q - 1 / (1 - q) + neighbor_dlogfdq]
            else:
                logg_array = []
                dloggdp_array = []
                dloggdq_array = []
            for cx in range(c + 1):
                for mx in range(m + 1):
                    if (cx > 0 or mx > 1) and (c - cx > 0 or m - mx > 1):
                        (
                            neighbor1_ll_genotype,
                            (neighbor1_dlogfdp, neighbor1_dlogfdq),
                        ) = CollapsedTree._ll_genotype(cx, mx, p, q)
                        (
                            neighbor2_ll_genotype,
                            (neighbor2_dlogfdp, neighbor2_dlogfdq),
                        ) = CollapsedTree._ll_genotype(c - cx, m - mx, p, q)
                        logg = (
                            np.log(p)
                            + 2 * np.log(1 - q)
                            + neighbor1_ll_genotype
                            + neighbor2_ll_genotype
                        )
                        dloggdp = 1 / p + neighbor1_dlogfdp + neighbor2_dlogfdp
                        dloggdq = -2 / (1 - q) + neighbor1_dlogfdq + neighbor2_dlogfdq
                        logg_array.append(logg)
                        dloggdp_array.append(dloggdp)
                        dloggdq_array.append(dloggdq)
            if not logg_array:
                raise ValueError("Zero likelihood event")
            else:
                logf_result = scs.logsumexp(logg_array)
                softmax_logg_array = scs.softmax(logg_array)
                dlogfdp_result = np.multiply(softmax_logg_array, dloggdp_array).sum()
                dlogfdq_result = np.multiply(softmax_logg_array, dloggdq_array).sum()

        return (logf_result, np.array([dlogfdp_result, dlogfdq_result]))

    def ll(
        self,
        p: np.float64,
        q: np.float64,
    ) -> Tuple[np.float64, np.ndarray]:
        r"""Log likelihood of branching process parameters :math:`(p, q)` given tree topology :math:`T` and genotype abundances :math:`A`.

        .. math::
            \ell(p, q; T, A) = \log\mathbb{P}(T, A \mid p, q)

        Args:
            p: branching probability
            q: mutation probability

        Returns:
            Log likelihood :math:`\ell(p, q; T, A)` and its gradient :math:`\nabla\ell(p, q; T, A)`
        """
        if self.tree is None:
            raise ValueError("tree data must be defined to compute likelihood")
        return _lltree(self._cm_counts, p, q)

    def mle(self, **kwargs) -> Tuple[np.float64, np.float64]:
        r"""Maximum likelihood estimate of :math:`(p, q)`.

        .. math::
            (p, q) = \arg\max_{p,q\in [0,1]}\ell(p, q)

        Args:
            kwargs: keyword arguments passed along to the log likelihood :meth:`CollapsedTree.ll`

        Returns:
            Tuple :math:`(p, q)` with estimated branching probability and estimated mutation probability
        """
        return _mle_helper(self.ll, **kwargs)

    def simulate(self, p: np.float64, q: np.float64, root: bool = True):
        r"""Simulate a collapsed tree as an infinite type Galton-Watson process
        run to extintion, with branching probability :math:`p` and mutation
        probability :math:`q`. Overwrites existing tree attribute.

        Args:
            p: branching probability
            q: mutation probability
            root: flag indicating simulation is being run from the root of the tree, so we should update tree attributes (should usually be ``True``)
        """
        c, m = self._simulate_genotype(p, q)
        self.tree = ete3.TreeNode()
        self.tree.add_feature("abundance", c)
        for _ in range(m):
            # ooooh, recursion
            child = CollapsedTree()
            child.simulate(p, q, root=False)
            child = child.tree
            child.dist = 1
            self.tree.add_child(child)

        if root:
            # create list of (c, m) for each node
            self._build_cm_counts()

    def __repr__(self):
        r"""return a string representation for printing."""
        return str(self.tree)

    def render(
        self,
        outfile: str,
        scale: float = None,
        branch_margin: float = 0,
        node_size: float = None,
        idlabel: bool = False,
        colormap: Dict = None,
        frame: int = None,
        position_map: List = None,
        chain_split: int = None,
        frame2: int = None,
        position_map2: List = None,
        show_support: bool = False,
    ):
        r"""Render to tree image file.

        Args:
            outfile: file name to render to, filetype inferred from suffix, .svg for color
            scale: branch length scale in pixels (set automatically if ``None``)
            branch_margin: additional leaf branch separation margin, in pixels, to scale tree width
            node_size: size of nodes in pixels (set according to abundance if ``None``)
            idlabel: label nodes with seq ids, and write sequences of all nodes to a fasta file with same base name as ``outfile``
            colormap: dictionary mapping node names to color names or to dictionaries of color frequencies
            frame: coding frame for annotating amino acid substitutions
            position_map: mapping of position names for sequence indices, to be used with substitution annotations and the ``frame`` argument
            chain_split: if sequences are a concatenation two gene sequences, this is the index at which the 2nd one starts (requires ``frame`` and ``frame2`` arguments)
            frame2: coding frame for 2nd sequence when using ``chain_split``
            position_map2: like ``position_map``, but for 2nd sequence when using ``chain_split``
            show_support: annotate bootstrap support if available
        """
        if frame is not None and frame not in (1, 2, 3):
            raise RuntimeError("frame must be 1, 2, or 3")

        def my_layout(node):
            if colormap is None or node.name not in colormap:
                circle_color = "lightgray"
            else:
                circle_color = colormap[node.name]
            text_color = "black"

            if node_size is None:
                node_size2 = max(1, 10 * np.sqrt(node.abundance))
                if isinstance(circle_color, str):
                    C = ete3.CircleFace(
                        radius=node_size2,
                        color=circle_color,
                        label={"text": str(node.abundance), "color": text_color}
                        if node.abundance > 0
                        else None,
                    )
                    C.rotation = -90
                    C.hz_align = 1
                    ete3.faces.add_face_to_node(C, node, 0)
                else:
                    P = ete3.PieChartFace(
                        [100 * x / node.abundance for x in circle_color.values()],
                        2 * node_size2,
                        2 * node_size2,
                        colors=[
                            (color if color != "None" else "lightgray")
                            for color in list(circle_color.keys())
                        ],
                        line_color=None,
                    )
                    T = ete3.TextFace(
                        " ".join([str(x) for x in list(circle_color.values())]),
                        tight_text=True,
                    )
                    T.hz_align = 1
                    T.rotation = -90
                    ete3.faces.add_face_to_node(P, node, 0, position="branch-right")
                    ete3.faces.add_face_to_node(T, node, 1, position="branch-right")
            elif node.abundance:
                T = ete3.TextFace(node.abundance)
                T.rotation = -90
                T.hz_align = 1
                ete3.faces.add_face_to_node(
                    T,
                    node,
                    0,
                    position="branch-top",
                )

            if idlabel:
                T = ete3.TextFace(node.name, tight_text=True, fsize=6)
                T.rotation = -90
                T.hz_align = 1
                ete3.faces.add_face_to_node(
                    T,
                    node,
                    1 if isinstance(circle_color, str) else 2,
                    position="branch-bottom",
                )

        # we render on a copy, so faces are not permanent
        tree_copy = self.tree.copy(method="deepcopy")
        for node in tree_copy.traverse():
            nstyle = ete3.NodeStyle()
            if colormap is None or node.name not in colormap:
                nstyle["fgcolor"] = "lightgray"
            else:
                nstyle["fgcolor"] = colormap[node.name]
            if node_size is not None:
                nstyle["size"] = node_size
            else:
                nstyle["size"] = 0
            if node.up is not None:
                if "sequence" in tree_copy.features and set(
                    node.sequence.upper()
                ) == set("ACGT"):
                    if frame is not None:
                        if chain_split is not None and frame2 is None:
                            raise ValueError(
                                "must define frame2 when using chain_split"
                            )
                        if frame2 is not None and chain_split is None:
                            raise ValueError(
                                "must define chain_split when using frame2"
                            )
                        # loop over split heavy/light chain subsequences
                        for start, end, framex, position_mapx in (
                            (0, chain_split, frame, position_map),
                            (chain_split, None, frame2, position_map2),
                        ):
                            if start is not None:
                                seq = node.sequence[start:end]
                                aa = Seq(
                                    seq[
                                        (framex - 1) : (
                                            framex
                                            - 1
                                            + (3 * ((len(seq) - (framex - 1)) // 3))
                                        )
                                    ]
                                ).translate()
                                seq = node.up.sequence[start:end]
                                aa_parent = Seq(
                                    seq[
                                        (framex - 1) : (
                                            framex
                                            - 1
                                            + (3 * ((len(seq) - (framex - 1)) // 3))
                                        )
                                    ]
                                ).translate()
                                mutations = [
                                    f"{aa1}{pos if position_mapx is None else position_mapx[pos]}{aa2}"
                                    for pos, (aa1, aa2) in enumerate(zip(aa_parent, aa))
                                    if aa1 != aa2
                                ]
                                if mutations:
                                    T = ete3.TextFace(
                                        "\n".join(mutations),
                                        fsize=6,
                                        tight_text=False,
                                        ftype="Courier",
                                    )
                                    if start == 0:
                                        T.margin_top = 6
                                    else:
                                        T.margin_bottom = 6
                                    T.rotation = -90
                                    node.add_face(
                                        T,
                                        0,
                                        position="branch-bottom"
                                        if start == 0
                                        else "branch-top",
                                    )
                                if "*" in aa:
                                    nstyle["hz_line_color"] = "red"
            node.set_style(nstyle)

        ts = ete3.TreeStyle()
        ts.scale = scale
        ts.branch_vertical_margin = branch_margin
        ts.show_leaf_name = False
        ts.rotation = 90
        ts.draw_aligned_faces_as_table = False
        ts.allow_face_overlap = True
        ts.layout_fn = my_layout
        ts.show_scale = True
        ts.show_branch_support = show_support
        # if we labelled seqs, let's also write the alignment out so we have
        # the sequences (including of internal nodes)
        if idlabel:
            aln = MultipleSeqAlignment([])
            for node in tree_copy.traverse():
                aln.append(
                    SeqRecord(
                        Seq(str(node.sequence)),
                        id=str(node.name),
                        description=f"abundance={node.abundance}",
                    )
                )
            AlignIO.write(
                aln, open(os.path.splitext(outfile)[0] + ".fasta", "w"), "fasta"
            )
        return tree_copy.render(outfile, tree_style=ts)

    def feature_colormap(
        self,
        feature: str,
        cmap: str = "viridis",
        vmin: float = None,
        vmax: float = None,
    ) -> Dict[str, str]:
        r"""Generate a colormap based on a continuous tree feature.

        Args:
            feature: feature name (all nodes in tree attribute must have this feature)
            cmap: any matplotlib color palette: https://matplotlib.org/stable/gallery/color/colormap_reference.html
            vmin: minimum value for colormap (default to minimum of the feature over the tree)
            vmax: maximum value for colormap (default to maximum of the feature over the tree)

        Returns:
            Dictionary of node names to hex color strings, which may be used as the colormap in :meth:`gctree.CollapsedTree.render`
        """
        cmap = mp.cm.get_cmap(cmap)

        if vmin is None:
            vmin = np.nanmin([getattr(node, feature) for node in self.tree.traverse()])
        if vmax is None:
            vmax = np.nanmax([getattr(node, feature) for node in self.tree.traverse()])

        # define the minimum and maximum values for our colormap
        norm = mp.colors.Normalize(vmin=vmin, vmax=vmax)

        return {
            node.name: mp.colors.to_hex(cmap(norm(getattr(node, feature))))
            for node in self.tree.traverse()
        }

    def write(self, file_name: str):
        r"""Serialize to pickle file.

        Args:
            file_name: file name (.p suffix recommended)
        """
        with open(file_name, "wb") as f:
            pickle.dump(self, f)

    def newick(self, file_name: str):
        r"""Write to newick file.

        Args:
            file_name: file name (.nk suffix recommended)
        """
        self.tree.write(format=1, outfile=file_name)

    def compare(
        self, tree2: CollapsedTree, method: str = "identity"
    ) -> Union[bool, np.float64]:
        r"""Compare this tree to the other tree.

        Args:
            tree2: another object of this type
            method: comparison type (``identity``, ``MRCA``, or ``RF``)

        Returns:
            tree difference
        """
        if method == "identity":
            # we compare lists of seq, parent, abundance
            # return true if these lists are identical, else false
            list1 = sorted(
                (
                    node.sequence,
                    node.abundance,
                    node.up.sequence if node.up is not None else None,
                )
                for node in self.tree.traverse()
            )
            list2 = sorted(
                (
                    node.sequence,
                    node.abundance,
                    node.up.sequence if node.up is not None else None,
                )
                for node in tree2.tree.traverse()
            )
            return list1 == list2
        elif method == "MRCA":
            # matrix of hamming distance of common ancestors of taxa
            # takes a true and inferred tree as CollapsedTree objects
            taxa = [node.sequence for node in self.tree.traverse() if node.abundance]
            n_taxa = len(taxa)
            d = np.zeros(shape=(n_taxa, n_taxa))
            sum_sites = np.zeros(shape=(n_taxa, n_taxa))
            for i in range(n_taxa):
                nodei_true = self.tree.iter_search_nodes(sequence=taxa[i]).__next__()
                nodei = tree2.tree.iter_search_nodes(sequence=taxa[i]).__next__()
                for j in range(i + 1, n_taxa):
                    nodej_true = self.tree.iter_search_nodes(
                        sequence=taxa[j]
                    ).__next__()
                    nodej = tree2.tree.iter_search_nodes(sequence=taxa[j]).__next__()
                    MRCA_true = self.tree.get_common_ancestor(
                        (nodei_true, nodej_true)
                    ).sequence
                    MRCA = tree2.tree.get_common_ancestor((nodei, nodej)).sequence
                    d[i, j] = gctree.utils.hamming_distance(MRCA_true, MRCA)
                    sum_sites[i, j] = len(MRCA_true)
            return d.sum() / sum_sites.sum()
        elif method == "RF":
            tree1_copy = self.tree.copy(method="deepcopy")
            tree2_copy = tree2.tree.copy(method="deepcopy")
            for treex in (tree1_copy, tree2_copy):
                for node in list(treex.traverse()):
                    if node.abundance > 0:
                        child = ete3.TreeNode()
                        child.add_feature("sequence", node.sequence)
                        node.add_child(child)
            return tree1_copy.robinson_foulds(
                tree2_copy,
                attr_t1="sequence",
                attr_t2="sequence",
                unrooted_trees=True,
            )[0]
        else:
            raise ValueError("invalid distance method: " + method)

    def _get_split(self, node: ete3.TreeNode) -> Tuple[Set, Set]:
        r"""Return the bipartition resulting from clipping this node's edge
        above.

        Args:
            node: tree node

        Returns:
            A tuple of two sets
        """
        if node.get_tree_root() != self.tree:
            raise ValueError("node not found")
        if node == self.tree:
            raise ValueError("this node is the root (no split above)")
        parent = node.up
        taxa1 = []
        for node2 in node.traverse():
            if node2.abundance > 0 or node2 == self.tree:
                if isinstance(node2.name, str):
                    taxa1.append(node2.name)
                else:
                    taxa1.extend(node2.name)
        taxa1 = set(taxa1)
        node.detach()
        taxa2 = []
        for node2 in self.tree.traverse():
            if node2.abundance > 0 or node2 == self.tree:
                if isinstance(node2.name, str):
                    taxa2.append(node2.name)
                else:
                    taxa2.extend(node2.name)
        taxa2 = set(taxa2)
        parent.add_child(node)
        assert taxa1.isdisjoint(taxa2)
        assert taxa1.union(taxa2) == set(
            (
                name
                for node in self.tree.traverse()
                if node.abundance > 0 or node == self.tree
                for name in ((node.name,) if isinstance(node.name, str) else node.name)
            )
        )
        return tuple(sorted([taxa1, taxa2]))

    @staticmethod
    def _split_compatibility(split1, split2):
        diff = split1[0].union(split1[1]) ^ split2[0].union(split2[1])
        if diff:
            raise ValueError(
                "splits do not cover the same taxa\n" f"\ttaxa not in both: {diff}"
            )
        for partition1 in split1:
            for partition2 in split2:
                if partition1.isdisjoint(partition2):
                    return True
        return False

    def support(
        self,
        bootstrap_trees_list: List[CollapsedTree],
        weights: List[np.float64] = None,
        compatibility: bool = False,
    ):
        r"""Compute support from a list of bootstrap :class:`CollapsedTree` objects, and add to tree attibute.

        Args:
            bootstrap_trees_list: List of trees
            weights: weights for each tree, perhaps for weighting parsimony degenerate trees
            compatibility: counts trees that don't disconfirm the split.
        """
        for node in self.tree.get_descendants():
            split = self._get_split(node)
            support = 0
            compatibility_ = 0
            for i, tree in enumerate(bootstrap_trees_list):
                compatible = True
                supported = False
                for boot_node in tree.tree.get_descendants():
                    boot_split = tree._get_split(boot_node)
                    if (
                        compatibility
                        and compatible
                        and not self._split_compatibility(split, boot_split)
                    ):
                        compatible = False
                    if not compatibility and not supported and boot_split == split:
                        supported = True
                if supported:
                    support += weights[i] if weights is not None else 1
                if compatible:
                    compatibility_ += weights[i] if weights is not None else 1
            node.support = compatibility_ if compatibility else support

    def local_branching(self, tau=1, tau0=0.1):
        r"""Add local branching statistics (Neher et al. 2014) as tree node
        features to the ETE tree attribute.
        After execution, all nodes will have new features ``LBI``
        (local branching index) and ``LBR`` (local branching ratio, below Vs
        above the node)

        Args:
            tau: decay timescale for exponential filter
            tau0: effective branch length for branches with zero mutations
        """
        # the fixed integral contribution for clonal cells indicated by abundance annotations
        clone_contribution = tau * (1 - np.exp(-tau0 / tau))

        # post-order traversal to populate downward integral for each node
        for node in self.tree.traverse(strategy="postorder"):
            if node.is_leaf():
                node.add_feature(
                    "LB_down",
                    node.abundance * clone_contribution if node.abundance > 1 else 0,
                )
            else:
                node.add_feature(
                    "LB_down",
                    node.abundance * clone_contribution
                    + sum(
                        tau * (1 - np.exp(-child.dist / tau))
                        + np.exp(-child.dist / tau) * child.LB_down
                        for child in node.children
                    ),
                )

        # pre-order traversal to populate upward integral for each node
        for node in self.tree.traverse(strategy="preorder"):
            if node.is_root():
                node.add_feature("LB_up", 0)
            else:
                node.add_feature(
                    "LB_up",
                    tau * (1 - np.exp(-node.dist / tau))
                    + np.exp(-node.dist / tau) * (node.up.LB_up + node.up.LB_down),
                )

        # finally, compute LBI (LBR) as the sum (ratio) of upward and downward integrals at each node
        for node in self.tree.traverse():
            node.add_feature("LBI", node.LB_down + node.LB_up)
            node.add_feature(
                "LBR", node.LB_down / node.LB_up if not node.is_root() else np.nan
            )


def requires_dag(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if self._forest is None:
            raise NotImplementedError(
                f"CollapsedForest class method {func.__name__} may only be called on instances "
                "created from lists of ete3 trees. Instances created by simulation are not supported."
            )
        return func(self, *args, **kwargs)

    return wrapper


class CollapsedForest:
    r"""A collection of trees.

    We can intialize with a list of trees, each an instance of :class:`ete3.Tree`, or we can simulate the forest later.

    Attributes:
        n_trees: number of trees in forest
        parameters: fit branching process parameters, if mle has been run, otherwise None

    Args:
        forest: list of :class:`ete3.Tree`
        sequence_counts: a mapping of observed sequences to observed abundances
    """

    def __init__(
        self, forest: List[ete3.Tree] = None, sequence_counts: Mapping[str, int] = None
    ):
        if forest is not None:
            if len(forest) == 0:
                raise ValueError("passed empty tree list")
            if sequence_counts is None:
                raise ValueError(
                    "an abundance dictionary must be provided to the keyword argument sequence_counts"
                )
            # Collect stats for validation
            model_tree = disambiguate(forest[0].copy())
            counts = {node.name: node.abundance for node in model_tree.iter_leaves()}
            self._validation_stats = {
                "counts": counts,
                "root": model_tree.name,
                "parsimony_score": sum([node.dist for node in model_tree.traverse()]),
                "root_seq": model_tree.sequence,
                "leaf_seqs": {
                    node.sequence: node.name for node in model_tree.get_leaves()
                },
            }
            # Making this a private variable so that trying to access forest
            # attribute as before won't just give a confusing type or
            # attribute error.
            self._forest = _make_dag(forest, sequence_counts=sequence_counts)
            self.n_trees = self._forest.count_trees()
        else:
            self._forest = None
            self.n_trees = 0
            self._validation_stats = None
        self._cm_countlist = None
        self._ctrees = None
        self.parameters = None
        self._cm = None

    def simulate(self, p: np.float64, q: np.float64, n_trees: int):
        r"""Simulate a forest of collapsed trees. Overwrites existing forest attribute.

        Args:
            p: branching probability
            q: mutation probability
            n_trees: number of trees
        """
        self._forest = None
        self._ctrees = [CollapsedTree() for _ in range(n_trees)]
        self.n_trees = n_trees
        for tree in self._ctrees:
            tree.simulate(p, q)
        self._cm_countlist = tuple(
            coll.Counter([tree._cm_counts for tree in self._ctrees]).items()
        )

    def ll(
        self,
        p: np.float64,
        q: np.float64,
        marginal: bool = False,
    ) -> Tuple[np.float64, np.ndarray]:
        r"""Log likelihood of branching process parameters :math:`(p, q)` given tree topologies :math:`T_1, \dots, T_n` and corresponding genotype abundances vectors :math:`A_1, \dots, A_n` for each of :math:`n` trees in the forest.

        If ``marginal=False`` (the default), compute the joint log likelihood

        .. math::
            \ell(p, q; T, A) = \sum_{i=1}^n\log\mathbb{P}(T_i, A_i \mid p, q),

        otherwise compute the marginal log likelihood

        .. math::
            \ell(p, q; T, A) = \log\left(\sum_{i=1}^n\mathbb{P}(T_i, A_i \mid p, q)\right).

        Args:
            p: branching probability
            q: mutation probability
            marginal: compute the marginal likelihood over trees, otherwise compute the joint likelihood of trees

        Returns:
            Log likelihood :math:`\ell(p, q; T, A)` and its gradient :math:`\nabla\ell(p, q; T, A)`
        """
        if self._cm_countlist is None:
            if self._forest is not None:
                cmcount_dagfuncs = _cmcounter_dagfuncs()

                def to_tuple(mset):
                    # When there's unobserved root unifurcation, augment with
                    # pseudocount.
                    if (0, 1) in mset:
                        assert mset[(0, 1)] == 1
                        mset = mset - {(0, 1)} + {(1, 1)}
                    return tuple(mset.items())

                cmcounters = self._forest.weight_count(**cmcount_dagfuncs)
                self._cm_countlist = [
                    (to_tuple(mset), mult) for mset, mult in cmcounters.items()
                ]
                # an iterable containing tuples `(cm_counts, mult)`, where `cm_counts`
                # is an iterable describing a tree, and `mult` is the number of trees in the forest
                # which can be described with the iterable `cm_counts`.
            elif self._ctrees is not None:
                self._cm_countlist = tuple(
                    coll.Counter([tree._cm_counts for tree in self._ctrees]).items()
                )
            else:
                raise ValueError("forest data must be defined to compute likelihood")

        terms = [
            [_lltree(cmcounts, p, q), count] for cmcounts, count in self._cm_countlist
        ]
        ls = np.array([term[0][0] for term in terms])
        grad_ls = np.array([term[0][1] for term in terms])
        # This can be done ahead of time
        count_ls = np.array([term[1] for term in terms])
        if marginal:
            # we need to find the smallest derivative component for each
            # coordinate, then subtract off to get positive things to logsumexp
            grad_l = []
            for j in range(len((p, q))):
                i_prime = grad_ls[:, j].argmin()
                b = (grad_ls[:, j] - grad_ls[i_prime, j]) * count_ls
                # believe it or not, logsumexp can't handle 0 in b
                # when np.seterr(underflow='raise') on newer processors:
                grad_l.append(
                    grad_ls[i_prime, j]
                    + np.exp(
                        scs.logsumexp((ls - ls[i_prime])[b != 0], b=b[b != 0])
                        - scs.logsumexp(ls - ls[i_prime], b=count_ls)
                    )
                )
            # count_ls shouldn't have any zeros in it...
            return (-np.log(count_ls.sum()) + scs.logsumexp(ls, b=count_ls)), np.array(
                grad_l
            )
        else:
            return (ls * count_ls).sum(), np.array(
                [(grad_ls[:, 0] * count_ls).sum(), (grad_ls[:, 1] * count_ls).sum()]
            )

    def mle(self, **kwargs) -> Tuple[np.float64, np.float64]:
        r"""Maximum likelihood estimate of :math:`(p, q)`.

        .. math::
            (p, q) = \arg\max_{p,q\in [0,1]}\ell(p, q)

        Args:
            kwargs: keyword arguments passed along to the log likelihood :meth:`CollapsedForest.ll`

        Returns:
            Tuple :math:`(p, q)` with estimated branching probability and estimated mutation probability
        """
        self.parameters = _mle_helper(self.ll, **kwargs)
        return self.parameters

    @requires_dag
    def filter_trees(
        self,
        priority_weights: Sequence[float] = None,
        verbose: bool = False,
        outbase: str = None,
        mutability_file: str = None,
        substitution_file: str = None,
        isotypemap: Mapping[str, str] = None,
        isotypemap_file: str = None,
        idmap: Mapping[str, Set[str]] = None,
        idmap_file: str = None,
        isotype_names: Sequence[str] = None,
        img_type: str = "svg",
    ) -> CollapsedForest:
        """Filter trees according to specified criteria.

        Trim the forest to minimize a linear
        combination of branching process likelihood, isotype parsimony score,
        mutability parsimony score, and number of alleles, with coefficients
        provided in the argument `priority_weights`, in that order.

        To compute each tree's combined score, a linear transformation is applied to each trait,
        so that the best observed value is mapped to 0, and the worst observed value is mapped to
        1. The tree score is the weighted sum of transformed traits, using passed coefficients.

        Args:
            priority_weights: A list or tuple of coefficients for prioritizing tree weights.
                The order of coefficients is: branching process likelihood, isotype
                parsimony score, mutability parsimony score, and number of alleles.
                If priority_weights is not provided, trees will be ranked lexicographically
                by traits, in the same order.
            verbose: print information about trimming
            outbase: file name stem for a file with information for each tree in the DAG, and rank plots of likelihoods.
                If not provided, no such files will be written.
            mutability_file: A mutability model
            substitution_file: A substitution model
            isotypemap: A mapping of sequences to isotypes
            isotypemap: A dictionary mapping original IDs to observed isotype names
            isotypemap_file: A csv file providing an `isotypemap`
            idmap: A dictionary mapping unique sequence IDs to sets of original IDs of observed sequences
            idmap_file: A csv file providing an `idmap`
            isotype_names: A sequence of isotype names containing values in `isotypemap`, in the correct switching order
            img_type: Format to be used for output plots, if `outbase` string is provided.

        Returns:
            The trimmed forest, containing all optimal trees according to the specified criteria.
        """
        dag = self._forest
        if self.parameters is None:
            self.mle(marginal=True)
        elif verbose:
            print("Skipping parameter fitting")
        p, q = self.parameters
        placeholder_dagfuncs = hdag.utils.AddFuncDict(
            {
                "start_func": lambda n: 0,
                "edge_weight_func": lambda n1, n2: 0,
                "accum_func": sum,
            },
            names="PlaceholderWeight",
        )
        try:
            iso_funcs = _isotype_dagfuncs(
                isotypemap_file=isotypemap_file,
                isotypemap=isotypemap,
                idmap_file=idmap_file,
                idmap=idmap,
                isotype_names=isotype_names,
            )
            if verbose:
                print("Isotype parsimony will be used as a ranking criterion")
        except ValueError:
            iso_funcs = placeholder_dagfuncs + placeholder_dagfuncs
        ll_dagfuncs = _ll_cmcount_dagfuncs(p, q)
        if mutability_file and substitution_file:
            if verbose:
                print("Mutation model parsimony will be used as a ranking criterion")
            mut_funcs = _mutability_dagfuncs(
                mutability_file=mutability_file, substitution_file=substitution_file
            )
        else:
            mut_funcs = placeholder_dagfuncs
        allele_funcs = _allele_dagfuncs()
        if priority_weights:
            # a vector of relative weights, for ll, isotype pars, mutability_pars, alleles
            # This is possible because all weights are additive up the tree,
            # and the transformations are strictly increasing/decreasing.
            kwargls = [ll_dagfuncs, iso_funcs, mut_funcs, allele_funcs]
            minmaxls = [
                (
                    dag.optimal_weight_annotate(**kwargs, optimal_func=min),
                    dag.optimal_weight_annotate(**kwargs, optimal_func=max),
                )
                for kwargs in kwargls
            ]
            minmaxls = [
                (mn, mx) if isinstance(kwargs.names, str) else (mn[0], mx[0])
                for (mn, mx), kwargs in zip(minmaxls, kwargls)
            ]

            def transformation(mn, mx):
                if mx - mn < 0.0001:

                    def func(n):
                        return n - mn

                else:

                    def func(n):
                        return (n - mn) / (mx - mn)

                return func

            transformations = [transformation(*minmax) for minmax in minmaxls]
            # switch first transformation so that we maximize ls, not minimize:
            f = transformations[0]
            transformations[0] = lambda n: -f(n) + 1

            def minfunckey(weighttuple):
                ll, _, isotypepars, _, mutabilitypars, alleles = weighttuple
                weightlist = [ll, isotypepars, mutabilitypars, alleles]
                return sum(
                    [
                        priority * transform(weight)
                        for priority, transform, weight in zip(
                            priority_weights, transformations, weightlist
                        )
                    ]
                )

        else:

            def minfunckey(weighttuple):
                ll, _, isotypepars, _, mutabilitypars, alleles = weighttuple
                # Sort output by likelihood, then isotype parsimony, then mutability score
                return (-ll, isotypepars, mutabilitypars)

        # Filter by likelihood, isotype parsimony, mutability,
        # and make ctrees, cforest, and render trees
        dagweight_kwargs = ll_dagfuncs + iso_funcs + mut_funcs + allele_funcs
        trimdag = dag.copy()
        trimdag.trim_optimal_weight(
            **dagweight_kwargs, optimal_func=lambda l: min(l, key=minfunckey)
        )

        if outbase:
            dag_ls = list(dag.weight_count(**dagweight_kwargs).elements())
            # To clear _dp_data fields of their large cargo
            dag.optimal_weight_annotate(**placeholder_dagfuncs)
            dag_ls.sort(key=minfunckey)

            with open(outbase + ".forest_summary.log", "w") as fh:
                fh.write(f"Parameters: {(p, q)}\n")
                fh.write(
                    "tree\talleles\tlogLikelihood\t\tisotype_parsimony\tmutability_parsimony"
                    + ("\ttree score" if priority_weights else "")
                    + "\n"
                )
                for j, (l, _, isotypepars, _, mutabilitypars, alleles) in enumerate(
                    dag_ls, 1
                ):
                    if priority_weights:
                        treescore = str(
                            minfunckey((l, 0, isotypepars, 0, mutabilitypars, alleles))
                        )
                    else:
                        treescore = ""
                    fh.write(
                        f"{j}\t{alleles}\t{l}\t{isotypepars}\t\t\t{mutabilitypars}\t{treescore}\n"
                    )

            # For use in later plots
            dag_l = [ls[0] for ls in dag_ls]

            # rank plot of likelihoods
            plt.figure(figsize=(6.5, 2))
            try:
                plt.plot(np.exp(dag_l), "ko", clip_on=False, markersize=4)
                plt.ylabel("gctree likelihood")
                plt.yscale("log")
                plt.ylim([None, 1.1 * max(np.exp(dag_l))])
            except FloatingPointError:
                plt.plot(dag_l, "ko", clip_on=False, markersize=4)
                plt.ylabel("gctree log-likelihood")
                plt.ylim([None, 1.1 * max(dag_l)])
            plt.xlabel("parsimony tree")
            plt.xlim([-1, len(dag_l)])
            plt.tick_params(axis="y", direction="out", which="both")
            plt.tick_params(
                axis="x", which="both", bottom="off", top="off", labelbottom="off"
            )
            plt.savefig(outbase + ".inference.likelihood_rank." + img_type)

        if verbose:
            print(f"params: {(p, q)}")
            print("stats for optimal trees:")
            print(
                "alleles\tlogLikelihood\t\tisotype_parsimony\tmutability_parsimony"
                + ("\ttreescore" if priority_weights else "")
            )
            # with format (ll, _, isotypepars, _, mutabilitypars, alleles)
            stats = trimdag.optimal_weight_annotate(
                **dagweight_kwargs, optimal_func=lambda l: min(l, key=minfunckey)
            )
            print(
                f"{stats[5]}\t{stats[0]}\t{stats[2]}\t\t\t{stats[4]}\t"
                + str(minfunckey(stats) if priority_weights else "")
            )

        return self._trimmed_self(trimdag)

    @requires_dag
    def n_topologies(self) -> int:
        """Count the number of topology classes, ignoring internal node sequences"""
        return self._forest.count_topologies(collapse_leaves=True)

    @requires_dag
    def iter_topology_classes(self):
        """Sort trees by topology class

        Returns:
            A generator of CollapsedForest objects, each containing trees with the same topology,
                ignoring internal node labels. CollapsedForests will be yielded in reverse-order
                of the number of trees in each topology class, so that each CollapsedForest will
                contain at least as many trees as the one that follows."""
        topology_dagfuncs = hdag.utils.make_newickcountfuncs(
            internal_labels=False, collapse_leaves=True
        )
        ctopologies = list(self._forest.weight_count(**topology_dagfuncs).items())
        # yield classes with most members first
        ctopologies.sort(key=lambda t: -t[1])
        for ctopology, count in ctopologies:
            tdag = self._forest.copy()
            tdag.trim_topology(ctopology, collapse_leaves=True)
            yield self._trimmed_self(tdag)

    def sample_tree(self) -> CollapsedTree:
        """Sample a random CollapsedTree from the forest"""
        if self._ctrees is not None:
            return random.choice(self._ctrees)
        elif self._forest is not None:
            return self._clade_tree_to_ctree(self._forest.sample())
        else:
            raise ValueError("Cannot sample trees from an empty forest")

    def _trimmed_self(self, dag):
        """Create a new CollapsedForest object from a subset of the current one's
        history DAG, properly initializing validation_stats and n_trees"""
        newforest = CollapsedForest()
        newforest._validation_stats = self._validation_stats.copy()
        newforest.n_trees = dag.count_trees()
        newforest._forest = dag
        newforest.parameters = self.parameters
        return newforest

    def _clade_tree_to_ctree(
        self,
        clade_tree: hdag.HistoryDag,
    ) -> CollapsedTree:
        """Create and validate :meth:`CollapsedTree` object from tree-shaped history DAG.

        Uses self._validation_stats dictionary, if available, for validation. Dictionary keys are:
            root: The expected root name for the resulting tree (for validation)
            counts: Expected node abundances for each node name (for validation)
            parsimony_score: Expected Hamming parsimony score (for validation)
            root_seq: Expected root sequence (for validation)
            leaf_seqs: Expected leaf names, keyed by leaf sequences (for validation)

        Args:
            clade_tree: A tree-shaped history DAG, like that returned by :meth:`historydag.HistoryDag.sample`

        Returns:
            :meth:`CollapsedTree` object matching the topology of ``clade_tree``, but fully collapsed."""
        etetree = clade_tree.to_ete(
            name_func=lambda n: n.attr["name"],
            features=["sequence"],
            feature_funcs={"abundance": lambda n: n.attr["abundance"]},
        )

        ctree = CollapsedTree(etetree)
        # Here can do some validation on the tree:
        # root name:
        if self._validation_stats is not None:
            if self._validation_stats["root"] not in ctree.tree.name:
                raise RuntimeError(
                    f"collapsed tree should have root name '{self._validation_stats['root']}' but has instead {ctree.tree.name}"
                )
            # counts:
            counts = self._validation_stats["counts"]
            for node in etetree.iter_leaves():
                if node.name:
                    assert counts[node.name] == node.abundance
            for node in ctree.tree.traverse():
                if isinstance(node.name, tuple):
                    for name in node.name:
                        if name in counts:
                            assert node.abundance == counts[name]
                else:
                    if node.name in counts:
                        assert counts[node.name] == node.abundance
                    else:
                        assert node.abundance == 0

            # unnamed_seq issue:
            for node in ctree.tree.traverse():
                if node.name == "unnamed_seq":
                    raise RuntimeError("Some node names are missing")

            # Parsimony:
            if self._validation_stats["parsimony_score"] != sum(
                [node.dist for node in ctree.tree.traverse()]
            ):
                raise RuntimeError(
                    "History DAG tree parsimony score does not match parsimony score provided"
                )
            # Root sequence:
            if ctree.tree.sequence != self._validation_stats["root_seq"]:
                raise RuntimeError(
                    "History DAG root node sequence does not match root sequence provided"
                )
            # Leaf names:
            leaf_seqs = self._validation_stats["leaf_seqs"]
            # A dictionary of leaf sequences to leaf names
            observed_set = {
                node for node in ctree.tree.iter_descendants() if node.abundance > 0
            }
            for node in observed_set:
                if not node.is_root() and leaf_seqs[node.sequence] != node.name:
                    raise RuntimeError(
                        "History DAG tree leaf names don't match sequences"
                    )
            observed_seqs = {node.sequence for node in observed_set}
            nonroot_observed_seqs = observed_seqs - {ctree.tree.sequence}
            nonroot_leaf_seqs = set(leaf_seqs.keys()) - {ctree.tree.sequence}
            if nonroot_leaf_seqs != nonroot_observed_seqs:
                raise RuntimeError(
                    "Observed nonroot sequences in history DAG tree don't match "
                    "observed nonroot sequences passed in leaf_seqs."
                )
        return ctree

    def __repr__(self):
        r"""return a string representation for printing."""
        return f"n_trees = {self.n_trees}\n" "\n".join([str(tree) for tree in self])

    def __iter__(self):
        if self._ctrees is not None:
            yield from self._ctrees
        elif self._forest is not None:
            for cladetree in self._forest.get_trees():
                yield self._clade_tree_to_ctree(cladetree)
        else:
            yield from ()

    def __getstate__(self):
        # Avoid pickling large cached abundance data.
        # hDAG also defines its own getstate.
        d = self.__dict__.copy()
        d["_cm_countlist"] = None
        return d


def _mle_helper(
    ll: Callable[[np.float64, np.float64], Tuple[np.float64, np.ndarray]], **kwargs
) -> Tuple[np.float64, np.float64]:
    # initialization
    x_0 = (0.5, 0.5)
    bounds = ((1e-6, 1 - 1e-6), (1e-6, 1 - 1e-6))

    def f(x):
        """negative log likelihood."""
        return tuple(-y for y in ll(*x, **kwargs))

    grad_check = sco.check_grad(lambda x: f(x)[0], lambda x: f(x)[1], x_0)
    if grad_check > 1e-3:
        warnings.warn(
            "gradient mismatches finite difference " f"approximation by {grad_check}",
            RuntimeWarning,
        )
    result = sco.minimize(
        f,
        jac=True,
        x0=x_0,
        method="L-BFGS-B",
        options={"ftol": 1e-10},
        bounds=bounds,
    )
    # update params if None and optimization successful
    if not result.success:
        warnings.warn("optimization not sucessful, " + result.message, RuntimeWarning)
    return result.x[0], result.x[1]


def _lltree(cm_counts, p: np.float64, q: np.float64) -> Tuple[np.float64, np.ndarray]:
    r"""Log likelihood of branching process parameters :math:`(p, q)`
    .. math::
        \ell(p, q; T, A) = \log\mathbb{P}(T, A \mid p, q)

    Args:
        cm_counts: an iterable containing tuples `((c, m), n)` where `n` is the number of nodes
            in the tree with abundance `c` and `m` mutant clades
        p: branching probability
        q: mutation probability
    Returns:
        Log likelihood :math:`\ell(p, q; T, A)` and its gradient :math:`\nabla\ell(p, q; T, A)`
    """
    count_ls = [n for cm, n in cm_counts]
    cm_list = [cm for cm, n in cm_counts]
    # extract vector of function values and gradient components
    logf_data = [CollapsedTree._ll_genotype(c, m, p, q) for c, m in cm_list]
    logf = np.array([[x[0] * count_ls[i]] for i, x in enumerate(logf_data)]).sum()
    grad_ll_genotype = np.array(
        [x[1] * count_ls[i] for i, x in enumerate(logf_data)]
    ).sum(axis=0)
    return logf, grad_ll_genotype


def _cmcounter_dagfuncs():
    """Functions for accumulating frozen multisets of (c, m) pairs in trees in
    the DAG."""

    def edge_weight_func(n1, n2):
        if n1.label == n2.label and n2.is_leaf():
            # Then this is a leaf-adjacent node with nonzero abundance
            return multiset.FrozenMultiset()
        else:
            m = len(n2.clades)
            if frozenset({n2.label}) in n2.clades:
                m -= 1
            return multiset.FrozenMultiset([(n2.attr["abundance"], m)])

    def accum_func(cmsetlist: List[multiset.FrozenMultiset]):
        st = multiset.FrozenMultiset()
        for cmset in cmsetlist:
            st += cmset
        return st

    return hdag.utils.AddFuncDict(
        {
            "start_func": lambda n: multiset.FrozenMultiset(),
            "edge_weight_func": edge_weight_func,
            "accum_func": accum_func,
        },
        names="cmcounters",
    )


def _make_dag(trees, sequence_counts={}, from_copy=True):
    """Build a history DAG from ambiguous or disambiguated trees, whose nodes
    have abundance, name, and sequence attributes."""
    # preprocess trees so they're acceptable inputs
    # Assume all trees have fixed root sequence and fixed leaf sequences
    leaf_seqs = {node.sequence for node in trees[0].get_leaves()}

    sequence_counts = sequence_counts.copy()
    if from_copy:
        trees = [tree.copy() for tree in trees]
    if all(len(tree.children) > 1 for tree in trees):
        pass  # We're all good!
    elif trees[0].sequence not in leaf_seqs:
        if trees[0].sequence not in sequence_counts:
            sequence_counts[trees[0].sequence] = 0
        for tree in trees:
            newleaf = tree.add_child(name="", dist=0)
            newleaf.add_feature("sequence", trees[0].sequence)
            if tree.sequence != newleaf.sequence:
                raise ValueError(
                    "At least some trees unifurcate at root, but root sequence is not fixed."
                )
    else:
        # This should never happen in parsimony setting, when internal edges
        # are collapsed by sequence
        raise RuntimeError(
            "Root sequence observed, but the corresponding leaf is not a child of the root node. "
            "Gctree inference may give nonsensical results. Are you sure these are parsimony trees?"
        )

    dag = hdag.history_dag_from_etes(
        trees,
        ["sequence"],
        attr_func=lambda n: {
            "name": n.name,
        },
    )
    # If there are too many ambiguities at too many nodes, disambiguation will
    # hang. Need to have an alternative (disambiguate each tree before putting in dag):
    if (
        dag.count_trees(expand_count_func=hdag.utils.sequence_resolutions_count)
        / dag.count_trees()
        > 500000000
    ):
        warnings.warn(
            "Parsimony trees have too many ambiguities for disambiguation in history DAG. "
            "Disambiguating trees individually. History DAG may find fewer new parsimony trees."
        )
        trees = [disambiguate(tree) for tree in trees]
        dag = hdag.history_dag_from_etes(
            trees,
            ["sequence"],
            attr_func=lambda n: {
                "name": n.name,
            },
        )
    dag.explode_nodes(expand_func=hdag.utils.sequence_resolutions)
    # Look for (even) more trees:
    dag.add_all_allowed_edges(adjacent_labels=True)
    dag.trim_optimal_weight()
    dag.convert_to_collapsed()
    # Add abundances to attrs:
    for node in dag.preorder(skip_root=True):
        if node.label.sequence in sequence_counts:
            node.attr["abundance"] = sequence_counts[node.label.sequence]
        else:
            node.attr["abundance"] = 0

    if len(dag.hamming_parsimony_count()) > 1:
        raise RuntimeError(
            f"History DAG parsimony search resulted in parsimony trees of unexpected weights:\n {dag.hamming_parsimony_count()}"
        )
    for node in dag.preorder(skip_root=True):
        if (
            node.attr["abundance"] != 0
            and not node.is_leaf()
            and frozenset({node.label}) not in node.clades
        ):
            raise RuntimeError(
                "An internal node not adjacent to a leaf with the same label was found with nonzero abundance."
            )

    # give disambiguated sequences unique names
    sequences = {
        node.attr["name"]: node.label.sequence for node in dag.preorder(skip_root=True)
    }
    n_max = max([int(name) for name in sequences.keys() if name.isdigit()])
    namedict = {sequence: name for name, sequence in sequences.items()}
    for node in dag.preorder(skip_root=True):
        if node.label.sequence not in namedict:
            n_max += 1
            namedict[node.label.sequence] = str(n_max)
            node.attr["name"] = str(n_max)
    return dag


def _ll_cmcount_dagfuncs(p: np.float64, q: np.float64) -> hdag.utils.AddFuncDict:
    """A slower but more numerically stable equivalent to _ll_genotype_dagfuncs.

    Args:
        p, q: branching process parameters

    Returns:
        A :meth:`historydag.utils.AddFuncDict` which may be passed as keyword arguments
        to :meth:`historydag.HistoryDag.weight_count`, :meth:`historydag.HistoryDag.trim_optimal_weight`,
        or :meth:`historydag.HistoryDag.optimal_weight_annotate`
        methods to trim or annotate a :meth:`historydag.HistoryDag` according to branching process likelihood.
        Weight format is ``(log likelihood, cmcounts)`` where cmcounts is a FrozenMultiset containing abundance, mutant clade count pairs.
        To use these functions for DAG trimming, use an optimal function like
        `lambda l: max(l, key=lambda ll: ll[0])` for clarity, although min or max should work too.
    """

    funcdict = _cmcounter_dagfuncs()
    cmcount_weight_func = funcdict["edge_weight_func"]
    cmcount_addfunc = funcdict["accum_func"]

    @functools.lru_cache(maxsize=None)
    def ll(cmcounter: multiset.FrozenMultiset) -> np.float64:
        if cmcounter:
            if (0, 1) in cmcounter:
                cmcounter = cmcounter - {(0, 1)} + {(1, 1)}
            return _lltree(tuple(cmcounter.items()), p, q)[0]
        else:
            return np.float64(0.0)

    def edge_weight_func(n1, n2):
        st = cmcount_weight_func(n1, n2)
        return (ll(st), st)

    def accum_func(cmsetlist: List[Tuple[float, multiset.FrozenMultiset]]):
        st = cmcount_addfunc([t[1] for t in cmsetlist])
        return (ll(st), st)

    return hdag.utils.AddFuncDict(
        {
            "start_func": lambda n: (0, multiset.FrozenMultiset()),
            "edge_weight_func": edge_weight_func,
            "accum_func": accum_func,
        },
        names=("LogLikelihood", "cmcounters"),
    )


def _ll_genotype_dagfuncs(p: np.float64, q: np.float64) -> hdag.utils.AddFuncDict:
    """Return functions for counting tree log likelihood on the history DAG.

    Although these functions are fast, for numerical consistency use
    :meth:`_ll_cmcount_dagfuncs` instead.

    Args:
        p, q: branching process parameters

    Returns:
        A :meth:`historydag.utils.AddFuncDict` which may be passed as keyword arguments
        to :meth:`historydag.HistoryDag.weight_count`, :meth:`historydag.HistoryDag.trim_optimal_weight`,
        or :meth:`historydag.HistoryDag.optimal_weight_annotate`
        methods to trim or annotate a :meth:`historydag.HistoryDag` according to branching process likelihood.
        Weight format is ``float``.
    """

    def edge_weight_ll_genotype(n1: hdag.HistoryDagNode, n2: hdag.HistoryDagNode):
        """The _ll_genotype weight of the target node, unless it should be
        collapsed, then 0.

        Expects DAG to have abundances added so that each node has "abundance" key in attr dict.
        """
        if n2.is_leaf() and n2.label == n1.label:
            return np.float64(0.0)
        else:
            m = len(n2.clades)
            # Check if this edge should be collapsed, and reduce mutant descendants
            if frozenset({n2.label}) in n2.clades:
                m -= 1
            return CollapsedTree._ll_genotype(n2.attr["abundance"], m, p, q)[0]

    return hdag.utils.AddFuncDict(
        {
            "start_func": lambda n: np.float64(0),
            "edge_weight_func": edge_weight_ll_genotype,
            "accum_func": np.sum,
        }
    )


def _allele_dagfuncs() -> hdag.utils.AddFuncDict:
    """Return functions for filtering trees in a history DAG by allele count.

    The number of alleles in a tree is the number of unique sequences observed on nodes of that tree.

    Returns:
        A :meth:`historydag.utils.AddFuncDict` which may be passed as keyword arguments
        to :meth:`historydag.HistoryDag.weight_count`, :meth:`historydag.HistoryDag.trim_optimal_weight`,
        or :meth:`historydag.HistoryDag.optimal_weight_annotate`
        methods to trim or annotate a :meth:`historydag.HistoryDag` according to allele count.
        Weight format is ``int``.
    """
    return hdag.utils.AddFuncDict(
        {
            "start_func": lambda n: 0,
            "edge_weight_func": lambda n1, n2: n1.label != n2.label,
            "accum_func": sum,
        },
        names="NodeCount",
    )
