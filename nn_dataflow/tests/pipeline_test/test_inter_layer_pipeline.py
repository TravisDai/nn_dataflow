""" $lic$
Copyright (C) 2016-2017 by The Board of Trustees of Stanford University

This program is free software: you can redistribute it and/or modify it under
the terms of the Modified BSD-3 License as published by the Open Source
Initiative.

If you use this program in your research, we request that you reference the
TETRIS paper ("TETRIS: Scalable and Efficient Neural Network Acceleration with
3D Memory", in ASPLOS'17. April, 2017), and that you send us a citation of your
work.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the BSD-3 License for more details.

You should have received a copy of the Modified BSD-3 License along with this
program. If not, see <https://opensource.org/licenses/BSD-3-Clause>.
"""

import re

from nn_dataflow.core import InputLayer, ConvLayer, FCLayer, PoolingLayer
from nn_dataflow.core import InterLayerPipeline
from nn_dataflow.core import Network
from nn_dataflow.core import Option
from nn_dataflow.core import PhyDim2
from nn_dataflow.core import PipelineSegment

from . import TestPipelineFixture

class TestInterLayerPipeline(TestPipelineFixture):
    ''' Tests for InterLayerPipeline. '''

    def test_valid_args(self):
        ''' Valid arguments. '''
        ilp = InterLayerPipeline(self.net['net1'], self.batch_size,
                                 self.resource, max_util_drop=0.1)
        self.assertIs(ilp.network, self.net['net1'])
        self.assertEqual(ilp.batch_size, self.batch_size)
        self.assertIs(ilp.resource, self.resource)
        self.assertEqual(ilp.max_util_drop, 0.1)

    def test_invalid_network(self):
        ''' Invalid network. '''
        with self.assertRaisesRegexp(TypeError,
                                     'InterLayerPipeline: .*network.*'):
            _ = InterLayerPipeline(self.net['net1'].input_layer(),
                                   self.batch_size, self.resource)

    def test_invalid_resource(self):
        ''' Invalid resource. '''
        with self.assertRaisesRegexp(TypeError,
                                     'InterLayerPipeline: .*resource.*'):
            _ = InterLayerPipeline(self.net['net1'], self.batch_size,
                                   PhyDim2(1, 1))

    def test_invalid_max_util_drop(self):
        ''' Invalid max_util_drop. '''
        with self.assertRaisesRegexp(ValueError,
                                     'InterLayerPipeline: .*max_util_drop.*'):
            _ = InterLayerPipeline(self.net['net1'], self.batch_size,
                                   self.resource, max_util_drop=1.1)

        with self.assertRaisesRegexp(ValueError,
                                     'InterLayerPipeline: .*max_util_drop.*'):
            _ = InterLayerPipeline(self.net['net1'], self.batch_size,
                                   self.resource, max_util_drop=-0.1)

    def test_topological_order(self):
        ''' Topological order. '''
        for net in self.net.values():

            if not net.net_name.startswith('net'):
                continue

            ilp = self._make_ilp(net)

            for layer in net:
                vidx = ilp.dag_vertex_dict[layer]

                self.assertIn(layer, ilp.dag_vertex_list[vidx])

                # Layer is named by topological order.
                self.assertTrue(layer.startswith(str(vidx)))

            # Disjoint union.
            vs_list = [set(v) for v in ilp.dag_vertex_list]

            for idx, vs in enumerate(vs_list):
                for vs2 in vs_list[:idx]:
                    self.assertTrue(vs.isdisjoint(vs2))
            self.assertSetEqual(set.union(*vs_list), set(net))

    def test_vertex_no_merge_lr(self):
        ''' LocalRegionLayer has no previous layer to merge with. '''
        net = Network('tmp_net')
        net.set_input(InputLayer(30, 1))
        net.add('0', PoolingLayer(30, 1, 1))
        net.add('1', FCLayer(30, 40))
        net.add('1p', PoolingLayer(40, 1, 1))

        ilp = self._make_ilp(net)

        for layer in net:
            vidx = ilp.dag_vertex_dict[layer]

            self.assertIn(layer, ilp.dag_vertex_list[vidx])

            # Layer is named by topological order.
            self.assertTrue(layer.startswith(str(vidx)))

    def test_prev(self):
        ''' Previous relationship. '''
        for net in self.net.values():

            ilp = self._make_ilp(net)

            for vidx, prevs in ilp.dag_prev_dict.items():

                # Previous layers of the current vertex.
                prev_layers = set()
                v = ilp.dag_vertex_list[vidx]
                for l in v:
                    prev_layers.update(net.prev_layers(l)[0])
                prev_layers.difference_update(v)

                for pvidx in prevs:

                    # Previous vertices should be ordered before this vertex.
                    self.assertLess(pvidx, vidx)

                    # Previous vertex should have at least one previous layer.
                    if pvidx < 0:
                        self.assertIn(None, prev_layers)
                    else:
                        pv = ilp.dag_vertex_list[pvidx]
                        self.assertFalse(prev_layers.isdisjoint(pv))

    def test_next(self):
        ''' Next relationship. '''
        for net in self.net.values():

            ilp = self._make_ilp(net)

            for vidx, nexts in ilp.dag_next_dict.items():

                # Next layers of the current vertex.
                next_layers = set()
                if vidx < 0:
                    next_layers = set(net.first_layers())
                else:
                    v = ilp.dag_vertex_list[vidx]
                    for l in v:
                        next_layers.update(net.next_layers(l))
                    next_layers.difference_update(v)

                for nvidx in nexts:

                    # Next vertices should be ordered after this vertex.
                    self.assertGreater(nvidx, vidx)

                    # Next vertex should have at least one next layer.
                    nv = ilp.dag_vertex_list[nvidx]
                    self.assertFalse(next_layers.isdisjoint(nv))

    def test_match_prev_next(self):
        ''' Previous and next relationships match. '''
        for net in self.net.values():

            ilp = self._make_ilp(net)

            for vidx, prevs in ilp.dag_prev_dict.items():
                for pvidx in prevs:
                    self.assertIn(vidx, ilp.dag_next_dict[pvidx])

            for vidx, nexts in ilp.dag_next_dict.items():
                for nvidx in nexts:
                    self.assertIn(vidx, ilp.dag_prev_dict[nvidx])

    def test_gen_vseg(self):
        ''' _gen_vseg. '''
        # pylint: disable=protected-access

        # Simple case.
        ilp = self._make_ilp(self.net['net1'])
        num = len(ilp.dag_vertex_list)
        self.assertEqual(len(list(ilp._gen_vseg())),
                         (num + 1) * num // 2)

        # Linear case.
        # Number of different vsegs of n = 1 + ... + n
        ilp = self._make_ilp(self.net['net2'])
        num = len(ilp.dag_vertex_list)
        self.assertEqual(len(list(ilp._gen_vseg())),
                         (num + 1) * num // 2)

        # Fork case.
        ilp = self._make_ilp(self.net['net4'])
        vseg_list = list(ilp._gen_vseg())
        self.assertEqual(len(vseg_list), 34)

        # Multiple first layers.
        self.assertGreater(len(self.net['net3'].first_layers()), 1)
        ilp = self._make_ilp(self.net['net3'])
        vseg_list = list(ilp._gen_vseg())
        self.assertIn((0,), vseg_list)
        self.assertIn((1,), vseg_list)

        # Verify rules.
        ilp = self._make_ilp(self.net['net5'])
        vseg_list = list(ilp._gen_vseg())
        # Layers with no shared dependencies.
        self.assertNotIn((2, 3, 4), vseg_list)
        self.assertNotIn((8, 9), vseg_list)
        # Multiple previous layers.
        self.assertNotIn((5, 6, 7), vseg_list)
        self.assertNotIn((8, 9, 10), vseg_list)
        self.assertNotIn((10, 11, 12), vseg_list)
        # Multiple next layers.
        self.assertNotIn((0, 1, 2, 3), vseg_list)
        self.assertNotIn((3, 4), vseg_list)
        self.assertIn((3, 4, 5), vseg_list)
        self.assertNotIn((10, 11), vseg_list)

        # No duplicate.
        for net in self.net.values():
            ilp = self._make_ilp(net)
            vseg_list = list(ilp._gen_vseg())
            self.assertEqual(len(vseg_list), len(set(vseg_list)))

        # Real networks.
        ilp = self._make_ilp(self.net['zfnet'])
        self.assertEqual(len(ilp.dag_vertex_list), 8)
        vseg_list = list(ilp._gen_vseg())
        self.assertEqual(len(vseg_list), 36)

        ilp = self._make_ilp(self.net['vgg_net'])
        self.assertEqual(len(ilp.dag_vertex_list), 16)
        vseg_list = list(ilp._gen_vseg())
        self.assertEqual(len(vseg_list), 136)

        # Large networks with forks.
        for net_name in ['googlenet', 'resnet152']:
            net = self.net[net_name]

            ilp = self._make_ilp(net)
            vseg_list = list(ilp._gen_vseg())
            self.assertEqual(len(vseg_list), len(set(vseg_list)))

            # The number of different vsegs is between one and three times of
            # the number of layers.
            self.assertGreater(len(vseg_list), len(net))
            self.assertLessEqual(len(vseg_list), len(net) * 3)

    def test_gen_vseg_twice(self):
        ''' _gen_vseg twice. '''
        # pylint: disable=protected-access
        for net_name in self.net:
            if not net_name.startswith('net'):
                continue

            net = self.net[net_name]
            ilp = self._make_ilp(net)

            vseg_list_1 = list(ilp._gen_vseg())
            vseg_list_2 = list(ilp._gen_vseg())

            self.assertListEqual(vseg_list_1, vseg_list_2)

    def test_ordered_layer_list(self):
        ''' Get ordered_layer_list. '''

        # https://stackoverflow.com/a/4836734/5277823
        nat_key = lambda key: tuple(int(c) if c.isdigit() else c.lower()
                                    for c in re.split('([0-9]+)', key))

        for net_name in ['net1', 'net2', 'net3', 'net4', 'net5']:
            net = self.net[net_name]
            ilp = self._make_ilp(net)
            ord_list = ilp.ordered_layer_list()

            # In natural order.
            self.assertTrue(all(nat_key(l1) < nat_key(l2) for l1, l2
                                in zip(ord_list, ord_list[1:])))

    def test_gen_segment(self):
        ''' gen_segment(). '''
        for net_name in self.net:
            net = self.net[net_name]
            ilp = self._make_ilp(net)

            # No pipelining.
            options = Option()
            segs_n = set(ilp.gen_segment(options))
            for seg in segs_n:
                self.assertEqual(len(seg), 1)
                self.assertEqual(len(seg[0]), 1)
                self.assertIn(seg[0][0], net)

            # Spatial pipelining.
            options = Option(partition_interlayer=True)
            segs_sp = set(ilp.gen_segment(options))
            for seg in segs_sp:
                for ltpl in seg:
                    self.assertLessEqual(sum(1 for l in ltpl
                                             if isinstance(l, ConvLayer)),
                                         1)
            self.assertTrue(segs_sp.issuperset(segs_n))

            # Temporal pipelining.
            options = Option(hw_gbuf_save_writeback=True)
            segs_tp = set(ilp.gen_segment(options))
            for seg in segs_tp:
                self.assertEqual(len(seg), 1)
            self.assertTrue(segs_tp.issuperset(segs_n))

            # Spatial and temporal pipelining.
            options = Option(partition_interlayer=True,
                             hw_gbuf_save_writeback=True)
            segs_stp = set(ilp.gen_segment(options))
            self.assertSetEqual(segs_stp, segs_tp | segs_sp)
            # Only single-layer and single-vertex segments have the same
            # spatial and temporal pipelining.
            segs_intersect = segs_tp & segs_sp
            segs_single = segs_n
            segs_single |= set(PipelineSegment((v,), ilp.network,
                                               ilp.batch_size, ilp.resource)
                               for v in ilp.dag_vertex_list)
            self.assertTrue(segs_intersect.issubset(segs_single))

    def test_gen_segment_max_degree(self):
        ''' gen_segment() maximum degree. '''
        net = self.net['vgg_net']
        ilp = self._make_ilp(net)

        options = Option(partition_interlayer=True,
                         hw_gbuf_save_writeback=True,
                         layer_pipeline_max_degree=4)
        for segment in ilp.gen_segment(options):
            self.assertLessEqual(sum(1 if isinstance(net[l], ConvLayer) else 0
                                     for ltpl in segment for l in ltpl),
                                 4)

    def test_gen_segment_vseg(self):
        ''' gen_segment() vertex segment. '''

        for net_name in self.net:
            if not net_name.startswith('net'):
                continue
            net = self.net[net_name]

            ilp = self._make_ilp(net)
            options = Option(partition_interlayer=True)

            seg_set = set(ilp.gen_segment(options))
            self.assertTrue(seg_set)

            seg_v_set = set(self._gen_all_segment(net))
            self.assertTrue(seg_set.issubset(seg_v_set))

