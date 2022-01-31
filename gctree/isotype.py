import re
import pickle
import argparse
from pathlib import Path
from gctree.isotyping import (
    explode_idmap,
    isotype_tree,
    isotype_parsimony,
    default_isotype_order,
)


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Given gctree inference outputs, and a file mapping original\n"
            "sequence names (the original sequence ids referenced as values in\n"
            '`idmapfile`) with format "Original SeqID, Isotype", this utility\n\n'
            "* Adds observed isotypes to each observed node in the collapsed\n"
            "  trees output by gctree inference. If cells with the same sequence\n"
            "  but different isotypes are observed, then collapsed tree nodes\n"
            "  must be ‘exploded’ into new nodes with the appropriate isotypes\n"
            "  and abundances. Each unique sequence ID generated by gctree is\n"
            "  prepended to its observed isotype, and a new `isotyped.idmap`\n"
            "  mapping these new sequence IDs to original sequence IDs is \n"
            "  written in the output directory.\n"
            "* Resolves isotypes of unobserved ancestral genotypes in a way\n"
            "  that minimizes isotype switching and obeys isotype switching\n"
            "  order. If observed isotypes of an observed internal node and its\n"
            "  children violate switching order, then the observed internal node\n"
            "  is replaced with an unobserved node with the same sequence, and\n"
            "  the observed internal node is placed as a child leaf. This\n"
            "  procedure always allows switching order conflicts to be resolved,\n"
            "  and should usually increase isotype transitions required in the\n"
            "  resulting tree.\n"
            "* Renders each new collapsed tree with colors and labels\n"
            "  reflecting observed or inferred isotypes, and writes a fasta and\n"
            "  newick file just like the gctree inference pipeline.\n"
            "* Prints for each collapsed tree, the original branching process\n"
            "  log likelihood, the original node count, the isotype parsimony\n"
            "  score, and the new node count after isotype additions. The\n"
            "  isotype parsimony score is just a count of how many isotype\n"
            "  transitions are required along tree edges. Changes in node count\n"
            "  after isotype additions indicate that either observed nodes had\n"
            "  to be exploded based on observed isotypes, or isotype switching\n"
            "  order violations required internal nodes to be expanded as leaf\n"
            "  nodes.\n\n"
            "This tool doesn’t make any judgements about which tree is best.\n"
            "Tree output order is the same as in gctree inference: ranking is\n"
            "by log likelihood before isotype additions. A determination of\n"
            "which is the best tree is left to the user, based on likelihoods,\n"
            "isotype parsimony score, and changes in the number of nodes after\n"
            "isotype additions.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inference_log",
        type=str,
        help="filename for gctree inference log file which contains branching process parameters",
    )
    parser.add_argument(
        "idmapfile",
        type=str,
        help="filename for a csv file mapping sequence names to original sequence ids, like the one output by deduplicate.",
    )
    parser.add_argument(
        "isotype_mapfile",
        type=str,
        help="filename for a csv file mapping original sequence ids to observed isotypes"
        ". For example, each line should have the format 'somesequence_id, some_isotype'.",
    )
    parser.add_argument(
        "--trees",
        nargs='+',
        type=str,
        help="filenames for collapsed tree pickle files output by gctree inference",
    )
    parser.add_argument(
        "--isotype_names",
        type=str,
        default=None,
        help="A list of isotype names used in isotype_mapfile, in order of most naive to most differentiated."
        """ Default is equivalent to providing the argument ``--isotype_names IgM,IgG3,IgG1,IgA1,IgG2,IgG4,IgE,IgA2``""",
    )
    parser.add_argument(
        "--out_directory",
        type=str,
        default=None,
        help="Directory in which to place output. Default is working directory.",
    )
    return parser


def main(arg_list=None):
    isotype_palette = [
        "#a6cee3",
        "#1f78b4",
        "#b2df8a",
        "#33a02c",
        "#fb9a99",
        "#e31a1c",
        "#fdbf6f",
        "#ff7f00",
        "#cab2d6",
        "#6a3d9a",
        "#ffff99",
        "#b15928",
    ]
    args = get_parser().parse_args(arg_list)
    if args.out_directory:
        out_directory = args.out_directory + "/"
        p = Path(out_directory)
        if not p.exists():
            p.mkdir()
    else:
        out_directory = ""
    with open(args.isotype_mapfile, "r") as fh:
        isotypemap = dict(map(lambda x: x.strip(), line.split(",")) for line in fh)

    with open(args.idmapfile, "r") as fh:
        idmap = {}
        for line in fh:
            seqid, cell_ids = line.rstrip().split(",")
            cell_idset = {cell_id for cell_id in cell_ids.split(":") if cell_id}
            if len(cell_idset) > 0:
                idmap[seqid] = cell_idset

    parameters = tuple()
    with open(args.inference_log, "r") as fh:
        for line in fh:
            if re.match(r"params:", line):
                p = float(re.search(r"(?<=[\(])\S+(?=\,)", line).group())
                q = float(re.search(r"(?<=\,\s)\S+(?=[\)])", line).group())
                parameters = (p, q)
                break
    if len(parameters) != 2:
        raise RuntimeError("unable to find parameters in passed `inference_log` file.")

    ctrees = []
    for treefile in args.trees:
        with open(treefile, "rb") as fh:
            ctrees.append(pickle.load(fh))
    # parse the idmap file and the isotypemap file
    ctrees = tuple(
        sorted(ctrees, key=lambda tree: -tree.ll(*parameters)[0])
    )
    tree_stats = [
        [
            filename,
            ctree,
            ctree.ll(*parameters)[0],
            idx,
            sum(1 for _ in ctree.tree.traverse()),
        ]
        for filename, (idx, ctree) in zip(args.trees, enumerate(ctrees))
    ]
    if not args.isotype_names:
        isotype_names = default_isotype_order
    else:
        isotype_names = str(args.isotype_names).split(",")

    newidmap = explode_idmap(idmap, isotypemap)
    for ctree in ctrees:
        ctree.tree = isotype_tree(ctree.tree, newidmap, isotype_names)

    flattened_newidmap = {
        name + " " + str(isotype): cell_idset
        for name, cellid_map in newidmap.items()
        for isotype, cell_idset in cellid_map.items()
    }

    with open(out_directory + "isotyped.idmap", "w") as fh:
        for name, cellidset in flattened_newidmap.items():
            print(f"{name},{':'.join(cellidset)}", file=fh)

    for sublist in tree_stats:
        sublist.append(isotype_parsimony(sublist[0].tree))

    # Compute parsimony indices
    for index, sublist in enumerate(sorted(tree_stats, key=lambda slist: slist[2])):
        sublist.append(index)

    print(
        f"Parameters:\t{parameters}\n"
        "index\t ll\t\t\t original node count\t isotype parsimony\t new node count"
    )
    for (
        filename,
        ctree,
        likelihood,
        likelihood_idx,
        original_numnodes,
        parsimony,
        parsimony_idx,
    ) in tree_stats:
        print(
            f"{likelihood_idx + 1}\t {likelihood}\t {original_numnodes}\t\t\t {parsimony}\t\t\t {sum(1 for _ in ctree.tree.traverse())}"
        )
        colormap = {
            node.name: isotype_palette[node.isotype.isotype % len(isotype_palette)]
            for node in ctree.tree.traverse()
        }
        newfilename = filename + f"{likelihood_idx + 1}.isotype_parsimony.{int(parsimony)}"
        ctree.render(
            outfile=out_directory + newfilename + ".svg",
            colormap=colormap,
            idlabel=True,
        )
        ctree.newick(out_directory + newfilename + ".nk")
        with open(out_directory + newfilename + ".p", 'wb') as fh:
            fh.write(pickle.dumps(ctree))
