#!/bin/python3
import nltk
from nltk.corpus import stopwords
import logging
import itertools
import json
import argparse
import math

from tqdm import tqdm, trange
from collections import OrderedDict
from itertools import islice

import flr
import util
import ascore
import multiprocessing as mp

import os, sys
sys.path.insert(0, os.path.abspath("../../../"))
from perspective import utility

# NOTE: conceptually coming from "An unsupervised aspect detection model for sentiment analysis of reviews"

aspect_data = {}
named_entities = [] # keep track of these separately in case want to weight more heavily afterwards


def detect(input_path, output_path, support=0.0, target_count=-1, thread_count=-1, named_entity_recog=False, overwrite=False):
    global aspect_data
    logging.info("Aspect detection requested on tokenized documents '%s'...", input_path)
    logging.info("(Support level: %f)", support)

    if not utility.check_output_necessary(output_path + "/aspects.json", overwrite):
        return

    # TODO: the input document arrays should be of dictionaries with "text" being the content
    # TODO: really don't need count here, that should be property of docify not detection

    pos_path = input_path + "/pos.json"
    doc_sent_path = input_path + "/doc_sent.json"
    sent_doc_path = input_path + "/sent_doc.json"

    # make the output path if it doens't exist
    if not os.path.exists(output_path):
        os.makedirs(output_path)
        
    # load in 
    logging.info("Loading tokenization information...")
    with open(pos_path, 'r') as file_in:
        pos_sentences = json.load(file_in)
    with open(doc_sent_path, 'r') as file_in:
        document_sentences = json.load(file_in)
    with open(sent_doc_path, 'r') as file_in:
        sentence_documents = json.load(file_in)
    
    if not named_entity_recog:
        generate_candidates(pos_sentences)
        prune_stopword_candidates()

    # get named entities if requested
    if named_entity_recog:
        find_named_entities(pos_sentences)

    # prune based on support
    prune_by_support(support, document_sentences, sentence_documents)

    compute_flr(pos_sentences, thread_count)

    logging.info("Saving aspect data...")
    with open(output_path + "/aspects.json" , 'w') as file_out:
        json.dump(aspect_data, file_out)
    exit()

    compute_a_score(pos_sentences)

    # testing just what's top and what isn't
    sorted_aspects = OrderedDict(sorted(aspect_data, key = lambda x: aspect_data[x]["ascore"]))
    if target_count > 0 and len(sorted_aspects.keys()) > target_count:
        sorted_aspects = islice(sorted_aspects.items(), target_count)
        
    #for thing in sorted_aspects:
        #yep = aspect_data[thing]
        #print(yep["pos"], yep["ascore"])

    logging.info("Saving aspect data...")
    with open(output_path + "/aspects.json" , 'w') as file_out:
        json.dump(sorted_aspects, file_out)


# TODO: definitely move out
def generate_patterns():
    patterns = []

    # adjective noun combinations "JJ NN", "JJ NN NNS", etc.
    for i in range(1, 4):
        for pattern in itertools.product(["NN", "NNS"], repeat=i):
            build = ["JJ"]
            build.extend(pattern)
            patterns.append(build)

    patterns.append(["DT", "JJ"])
    
    for i in range(1, 3):
        for pattern in itertools.product(["NN", "NNS", "VBG"], repeat=i):
            build = ["DT"]
            build.extend(pattern)
            patterns.append(build)

    return patterns

def generate_candidates(pos_sentences):
    patterns = generate_patterns()
    logging.info("Generating aspect candidates...")

    index = 0
    for pos_sentence in tqdm(pos_sentences):

        # search for all the patterns
        detect_sentence_aspects(pos_sentence, ["NN", "NNS", "NNP"], index, False, 1)
        detect_sentence_aspects(pos_sentence, ["NN", "NNS", "NNP"], index, False, 2)
        detect_sentence_aspects(pos_sentence, ["NN", "NNS", "NNP"], index, False, 3)
        detect_sentence_aspects(pos_sentence, ["NN", "NNS", "NNP"], index, False, 4)

        for pattern in patterns:
            detect_sentence_aspects(pos_sentence, pattern, index, True)

        index += 1

# TODO: move out
def detect_sentence_aspects(pos_sentence, pattern, sentence_index, order_matters=True, count=-1):
    global aspect_data

    if order_matters:
        end = len(pos_sentence) - len(pattern)
    else:
        end = len(pos_sentence) - count

    if len(pattern) >= len(pos_sentence) or count >= len(pos_sentence):
        return

    for index, (word, pos) in enumerate(pos_sentence, 1):
        if index == end:
            break

        i = 0
        found = True

        # this is for specific sequences of tags ex: match exactly a pattern of "JJ NN NNS"
        if order_matters:
            # for each word type (in order, enforced by i) in the requested pattern
            for part in pattern:

                # is the word type at point i does not match point i in the pattern, NOPE it out of here
                if part != pos_sentence[index + i][1]:
                    found = False
                    break
                i += 1

        # this is for detecting any combinations of word types in pattern of "count" size
        else:
            # check each word in the sentence from index to maximum pattern size
            for part in pos_sentence[index:index + count]:

                # if the word type isn't anywhere in the pattern, NOPE it out of here
                if part[1] not in pattern:
                    found = False
                    break

        if found:

            aspect = None
            if order_matters:
                aspect = pos_sentence[index:index + len(pattern)]
            else:
                aspect = pos_sentence[index:index + count]
                
            # add to database as needed
            string_aspect = util.stringify_pos(aspect)
            if string_aspect not in aspect_data.keys():
                aspect_data[string_aspect] = {"count": 1, "sentences": [sentence_index], "pos": aspect}
            else:
                aspect_data[string_aspect]["count"] += 1
                aspect_data[string_aspect]["sentences"].append(sentence_index)

def find_named_entities(pos_sentences):
    logging.info("Finding named entities...")

    index = 0
    for pos_sentence in tqdm(pos_sentences):
        find_named_entities_in_sentence(pos_sentence, index)
        index += 1

    logging.info("%i named entities found", len(named_entities))

def find_named_entities_in_sentence(pos_sentence, sentence_index):
    global aspect_data
    global named_entities

    local_ne = []

    parse_tree = nltk.ne_chunk(pos_sentence, binary=True)
    for t in parse_tree.subtrees():
        if t.label() == "NE":
            # local_ne.append(list(t))
            ne = list(t)
            string_aspect = util.stringify_pos(ne)
            if string_aspect not in named_entities:
                named_entities.append(string_aspect)
            
            # add to aspect_data
            if string_aspect not in aspect_data.keys():
                aspect_data[string_aspect] = {"count": 1, "sentences": [sentence_index], "pos": ne}
            else:
                aspect_data[string_aspect]["count"] += 1
                aspect_data[string_aspect]["sentences"].append(sentence_index)
    

# thread_count of -1 means to autodetect
def compute_flr(pos_sentences, thread_count=-1):
    global aspect_data

    with mp.Manager() as manager:
        d = manager.dict(aspect_data)

        # https://www.machinelearningplus.com/python/parallel-processing-python/
        p = thread_count
        if thread_count == -1: p = mp.cpu_count()
        pool = mp.Pool(p)

        logging.info("Computing FLR scores on %i cores...", p)

        # run flr calculation in parallel
        results = []
        for rank in range(0, p):
            result = pool.apply_async(compute_flr_partition, args=(d, pos_sentences, rank, p), callback=collect_flr)
            #result.get()
            #results.append(result)
            
        for result in results:
            result.get()
        
        pool.close()
        pool.join()

    #for aspect in tqdm(aspect_data.keys()):
        #aspect_data[aspect]["flr"] = flr.flr(aspect_data[aspect]["pos"], pos_sentences, aspect_data)
        
    #for dictionary in results:
        #for key in dictionary.keys():
            #aspect_data[key]["flr"] = dictionary[key]

def collect_flr(result):
    global aspect_data

    # https://www.machinelearningplus.com/python/parallel-processing-python/
    #print("Result:")
    #print(result)
    
    for key in result.keys():
        aspect_data[key]["flr"] = result[key]


# parallel FLR calculation function
def compute_flr_partition(aspect_data, pos_sentences, rank, p):
    local_data = {}

    keys = list(aspect_data.keys())
    per = math.floor(len(keys) / p)
    
    start = per*rank
    end = per*(rank + 1)
    if rank == p - 1: end = len(keys)

    for aspect in tqdm(keys[start:end], desc="FLR core {0}".format(rank), position=rank):
        local_data[aspect] = flr.flr(aspect_data[aspect]["pos"], pos_sentences, aspect_data)

    return local_data
    

def prune_stopword_candidates():
    global aspect_data
    
    logging.info("Pruning stopword candidates...")
    logging.info("Pre-aspects: %i", len(list(aspect_data.keys())))

    for aspect in tqdm(list(aspect_data.keys())):
        for aspect_part in aspect_data[aspect]["pos"]:
            if aspect_part[0] in stopwords.words("english"):
                del(aspect_data[aspect])
                break
            
    logging.info("Post-aspects: %i", len(list(aspect_data.keys())))


    #prune_by_support(support, document_sentences, sentence_documents)
def prune_by_support(min_support, document_sentences, sentence_documents):
    global aspect_data

    logging.info("Pruning candidates by minimum document support %f...", min_support)

    minimum_count = int(min_support * len(document_sentences))
    logging.info("Minimum count of %i needed is %i", len(document_sentences), minimum_count)

    for aspect in tqdm(list(aspect_data.keys())):
        unique_docs = 0
        docs = []
        for sentence_index in aspect_data[aspect]["sentences"]:
            doc = sentence_documents[sentence_index]
            if doc not in docs:
                docs.append(doc)
                unique_docs += 1

        if unique_docs < minimum_count:
            del aspect_data[aspect]

    logging.info("Post-aspects: %i", len(list(aspect_data.keys())))


def compute_a_score(pos_sentences):
    global aspect_data

    logging.info("Computing A-scores...")

    for aspect in tqdm(aspect_data.keys()):
        score = ascore.a_score(aspect_data, aspect, len(pos_sentences))
        aspect_data[aspect]["ascore"] = score

def parse():
    """Handle all command line argument parsing.

    Returns the parsed args object from the parser
    """
    parser = argparse.ArgumentParser()
    parser = utility.add_common_parsing(parser)

    parser.add_argument(
        "-s",
        "--support",
        dest="support",
        type=float,
        required=False,
        default=0.0,
        metavar="<float>",
        help="The minimum percentage of documents an aspect must appear in",
    )
    
    parser.add_argument(
        "--target-count",
        dest="target_count",
        type=int,
        required=False,
        default=-1,
        metavar="<int>",
        help="The target number of aspects",
    )
    
    parser.add_argument(
        "--ner",
        dest="ner",
        action="store_true",
        help="Specify this flag to run named entity recognition during aspect extraction",
    )

    cmd_args = parser.parse_args()
    return cmd_args

if __name__ == "__main__":
    ARGS = parse()
    utility.init_logging(ARGS.log_path)
    input_path, output_path = utility.fix_paths(ARGS.experiment_path, ARGS.input_path, ARGS.output_path)

    detect(input_path, output_path, ARGS.support, ARGS.target_count, ARGS.workers, ARGS.ner, ARGS.overwrite)
