#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Read a PHYLIP-format file and produce an appropriate config file for passing
to `dnapars`.

`dnapars` doesn't play very well in a
pipeline.  It prompts the user for configuration information and reads
responses from stdin.  The config file generated by this script is
meant to mimic the responses to the expected prompts.

Typical usage is,

     $ mkconfig sequence.phy > dnapars.cfg
     $ dnapars < dnapars.cfg

"""
import os
import random
import argparse


def get_parser():
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument("phylip", help="PHYLIP input", type=str)
    parser.add_argument("treeprog", help="dnaml or dnapars", type=str)
    parser.add_argument(
        "--quick", action="store_true", help="quicker (less thourough) dnapars"
    )
    parser.add_argument(
        "--bootstrap",
        type=int,
        default=0,
        help="input is seqboot output with this many samples",
    )
    return parser


def main(arg_list=None):
    args = get_parser().parse_args(arg_list)
    print(os.path.realpath(args.phylip))  # phylip input file
    if args.treeprog == "seqboot":
        print("R")
        print(args.bootstrap)
        print("Y")
        print(
            str(1 + 2 * random.randint(0, 1000000))
        )  # random seed for bootstrap (odd integer)
        return
    print("J")
    print(str(1 + 2 * random.randint(0, 1000000)))
    print("10")
    if args.bootstrap:
        print("M")
        print("D")
        print(args.bootstrap)
    if args.treeprog == "dnapars":
        print("O")  # Outgroup root
        print(1)  # arbitrary root on first
        if args.quick:
            print("S")
            print("Y")
        print("4")
        print("5")
        print(".")
        print("Y")
    elif args.treeprog == "dnaml":
        print("O")  # Outgroup root
        print(1)  # arbitrary root on first
        print("R")  # gamma
        print("5")  # Reconstruct hypothetical seq
        print("Y")  # accept these
        print("1.41421356237")  # CV = sqrt(2) (alpha = .5)
        print("4")  # 4 catagories
    else:
        raise RuntimeError(
            "treeprog=" + args.treeprog + ' is not "dnaml", "dnapars", or "seqboot"'
        )
