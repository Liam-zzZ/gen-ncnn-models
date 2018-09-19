#! /usr/bin/env python
# coding: utf-8
import sys,os,re
from collections import OrderedDict
import ncnnops

class MyGraph:
    class MyNode:
        # op: str,
        # name: str,
        # input: str array,
        # attr: map{str->object},
        # weights : map{str->np.array}, # tensorflow NHWC, C was inner
        pass

    def __init__(self, mydict):
        self.nodedict = mydict
        self.sorted = []

    def output(self, fn):
        if self.sorted:
            nodeNames = self.sorted
        else:
            nodeNames = self.nodedict.keys()

        with open(fn, 'w') as f:
            for name in nodeNames:
                f.write(name + ' ' + self.nodedict[name].op)
                if hasattr(self.nodedict[name], 'lnum'):
                    f.write(' ' + str(self.nodedict[name].lnum))
                f.write('\n')
                input_norm = self.nodedict[name].input_norm
                for i in input_norm:
                    f.write('    ' + i + '\n')


    def topoSort(self, node, inputNodes, stopNodes = set()):
        # 0:unvisit, 1:visited, 2:visiting
        if node in stopNodes or self.nodedict[node].op in stopNodes:
            self.nodedict[node].status = 1
            return

        if self.nodedict[node].status != 0:
            return

        if node in inputNodes:
            self.nodedict[node].status = 1
            self.sorted.append(node)
            return

        self.nodedict[node].status = 2

        for adj in self.nodedict[node].input_norm:
            if adj in self.nodedict:
                self.topoSort(adj, inputNodes, stopNodes)
            else:
                print('node name %s of %s not exists' % (adj, node))

        self.nodedict[node].status = 1

        self.sorted.append(node)

    def extractSubGraph(self, input_nodes, output_nodes, stop_nodes = []):
        mygraph = self

        assert input_nodes and output_nodes, 'input_nodes or output_nodes should not be empty'


        # 合并子图
        #mygraph.substituteSubGraph()

        # 输出这个图
        mygraph.output('before_toposort.txt')

        ### 首先将节点进行排序
        for name in mygraph.nodedict:
            mygraph.nodedict[name].status = 0
        mygraph.sorted = []

        for output in output_nodes:
            if output in mygraph.nodedict:
                mygraph.topoSort(output, input_nodes, stop_nodes)


        # 定义输入节点
        if mygraph.type == 'tf':
            mygraph.defineInputNodes(input_nodes)

        # 定义输出节点
        #mygraph.defineOutputNodes(output_nodes)

        # 输出这个图
        mygraph.output('after_toposort.txt')


        ### 去掉所有的Identity节点，直接将引用指向其引用的节点
        mygraph.checkConsistency()

        ### 将op和const各自定义一个局部编号，用于生成不同的变量名。
        mygraph.assignLocalNumber()

        mygraph.output('after_assignlocal.txt')

    def generateSource(self, moduleName, cfgfile, weightfile):
        # const将在成员变量中进行声明，op会有一个声明，然后还会有一个调用。
        # 将某几个节点之间的graph转为一个类。
        # 类的名字由用户指定，
        # 该类有一个load函数，用于从硬盘载入相应的weights。
        # 该类有一个compute函数，用于从给定的输入计算其输出。其输入参数可以有0-n个，输出参数有0-n个。
        mygraph = self
        oplist = mygraph.getOpList()


        # 按照节点的顺序，生成声明代码，初始化代码，计算代码
        timecode = '''
  {
    clock_t t1 = clock();
    $compcode
    clock_t t2 = clock();
    printf("layer %s output whc=%dx%dx%d cost %f ms\\n", "${opName}",
        ${outVarName}.w, ${outVarName}.h, ${outVarName}.c, 1e3*float(t2-t1) / CLOCKS_PER_SEC);
  }
        '''

        declcode = ''
        initcode = ''
        compcode = ''
        modelcode = ''
        ncnn_config_file = open(cfgfile, 'w')
        magic = 7767517
        ncnn_config_file.write('%d\n' % magic)
        ncnn_config_file.write('%d %d\n' % (len(oplist), len(oplist)+1))
        ncnn_weight_file = open(weightfile, 'wb')

        for i, nodeName in enumerate(oplist):
            node = mygraph.nodedict[nodeName]
            op = node.op
            #print(op, node.name)
            if hasattr(ncnnops, op):
                obj = getattr(ncnnops, op)(mygraph, nodeName)
                declcode += obj.genDeclaration() + '\n'
                initcode += obj.genInitializeFun() + '\n'
                compcode_ = obj.genComputeFun() + '\n'
                obj.genWeightFun(ncnn_weight_file)
                modelcode += obj.genModelFun() + '\n'
                compcode += ncnnops.MyTemplate(timecode).safe_substitute(compcode=compcode_, **vars(obj))
            else:
                print("op", op, "not found in", nodeName)

        ncnn_config_file.write(modelcode)
        ncnn_weight_file.close()
        ncnn_config_file.close()

        code = ncnnops.MyTemplate(ncnnops.header).safe_substitute(
            moduleName = moduleName,
            declaration = declcode)
        with open('%s.hpp' % moduleName, 'w') as f:
            f.write(code)

        code = ncnnops.MyTemplate(ncnnops.source).safe_substitute(
            moduleName = moduleName,
            initializeBody = initcode,
            computeBody = compcode,
        )
        with open('%s.cpp' % moduleName, 'w') as f:
            f.write(code)


    def generateCaffe(self, modelfile, weightfile):
        mygraph = self
        oplist = mygraph.getOpList()
        import caffe_pb2
        import numpy as np

        def getBlob(weight):
            blob = caffe_pb2.BlobProto()
            blob.raw_data_type = caffe_pb2.FLOAT
            blob.raw_data = weight.data.tobytes()
            blob.shape.dim.extend(weight.shape)
            return blob

        net = caffe_pb2.NetParameter()
        for i, nodeName in enumerate(oplist):
            node = mygraph.nodedict[nodeName]
            op = node.op
            obj = getattr(ncnnops, op)(mygraph, nodeName)
            layer = caffe_pb2.LayerParameter()
            layer.name = node.name
            layer.top.extend([node.name])
            layer.bottom.extend(node.input_norm)

            if op == 'DarknetNet':
                layer.type = 'DataInput'
                layer.data_input_param.shape.add().dim.extend([1,node.channels,node.height,node.width])
                layer.data_input_param.source.extend(['./road-car-00.png'])
                layer.transform_param.mean_value.extend([0,0,0])
                layer.transform_param.scale = 1./255
            elif op == 'Conv2D' or op == 'DepthwiseConv2dNative':
                layer.type = 'Convolution'
                layer.convolution_param.kernel_size.extend([obj.kernel_size])
                layer.convolution_param.pad.extend([obj.pad])
                layer.convolution_param.stride.extend([obj.stride])
                layer.convolution_param.group = obj.groups
                layer.convolution_param.num_output = obj.filters
                layer.convolution_param.bias_term = obj.bias_term
                if len(obj.kernel.shape) == 3:
                    kernel = np.expand_dims(obj.kernel, 1)
                else:
                    kernel = obj.kernel
                layer.blobs.extend([getBlob(kernel)])
            elif op == 'FusedBatchNorm':
                bnlayer = caffe_pb2.LayerParameter()
                bnlayer.name = node.name + '_bn'
                bnlayer.batch_norm_param.eps = 0
                #bnlayer.batch_norm_param.scale_bias = 1
                bnlayer.bottom.extend(node.input_norm)
                bnlayer.top.extend([bnlayer.name])
                bnlayer.type = "BatchNorm"
                bnlayer.blobs.extend([getBlob(obj.mean)])
                bnlayer.blobs.extend([getBlob(obj.variance)])
                bnlayer.blobs.extend([getBlob(np.ones([1,], dtype=np.float32))])
                net.layer.extend([bnlayer])

                layer.type = 'Scale'
                layer.scale_param.bias_term = 1
                layer.bottom.pop()
                layer.bottom.extend([bnlayer.name])
                layer.blobs.extend([getBlob(obj.gamma)])
                layer.blobs.extend([getBlob(obj.beta)])
                #layer.blobs.extend([getBlob(np.ones([1, ], dtype=np.float32))])
            elif op == 'Leaky':
                layer.type = 'ReLU'
                layer.relu_param.negative_slope = float(obj.slope)
            elif op == 'BiasAdd':
                layer.type = 'Bias'
                layer.blobs.extend([getBlob(obj.bias)])
            elif op == 'DarknetRegion':
                layer.type = 'RegionLoss'
                layer.region_loss_param.classes = node.classes
                layer.region_loss_param.num = node.num
                layer.region_loss_param.softmax = node.softmax
                layer.region_loss_param.biases.extend(node.anchors)
                layer.include.add().phase = caffe_pb2.TRAIN

                outlayer = caffe_pb2.LayerParameter()
                outlayer.name = node.name
                outlayer.type = "RegionOutput"
                outlayer.bottom.extend(node.input_norm)
                outlayer.top.extend([node.name])
                outlayer.region_output_param.classes = node.classes
                outlayer.region_output_param.num = node.num
                outlayer.region_output_param.softmax = node.softmax
                outlayer.region_output_param.biases.extend(node.anchors)
                outlayer.include.add().phase = caffe_pb2.TEST
                net.layer.extend([outlayer])

                outlayer = caffe_pb2.LayerParameter()
                outlayer.name = 'dataout'
                outlayer.type = "DataOutput"
                outlayer.bottom.extend(['net_0', node.name])
                outlayer.data_output_param.data_type = caffe_pb2.DataOutputParameter.DETECTION
                outlayer.data_output_param.out_type = "SHOW0"
                outlayer.include.add().phase = caffe_pb2.TEST
                net.layer.extend([outlayer])

            else:
                print('cannot convert Layer ' + op + ' to caffe')
                #assert False

            #print("add layer ", layer)
            net.layer.extend([layer])

        def proto2str(proto):
            from google.protobuf import text_format
            return text_format.MessageToString(proto, float_format='-g')

        with open(weightfile, 'wb') as f:
            f.write(net.SerializeToString())

        with open(modelfile, 'w') as f:
            for l in net.layer:
                while len(l.blobs) > 0:
                    l.blobs.pop()
            f.write(proto2str(net))



    def defineInputNodes(self, input_nodes):
        ### 加入输入节点
        for nodeName in input_nodes:
          if nodeName in self.nodedict:
            node = self.nodedict[nodeName]
            node.op = "Placeholder"
            node.input_norm = []
            node.input = []
          else:
            node = MyGraph.MyNode()
            node.input = []
            node.input_norm = []
            node.op = "Placeholder"
            node.name = nodeName
            node.attr = {}
            assert nodeName not in self.sorted
            self.sorted.insert(0, nodeName)
            self.nodedict[nodeName] = node


    def defineOutputNodes(self, output_nodes):
        pass

    def substituteSubGraph(self):
        mygraph = self.nodedict
        for nodeName in mygraph.keys():
            node = mygraph[nodeName]
            if node.op == 'Maximum':
                mulName = node.input[0]
                maxInName = node.input[1]
                assert mygraph[mulName].op == 'Mul'
                mulConstName = mygraph[mulName].input[0]
                mulInName = mygraph[mulName].input[1]
                assert mulInName == maxInName
                node.op = 'Leaky'
                node.input = node.input_norm = [mulInName]
                node.slope = ncnnops.Op.parseConst(mygraph[mulConstName])


    def checkConsistency(self):
        """
        主要是要将图做好兼容性。保证每一个被引用的节点都存在。
        去掉引用不存在的节点
        修改Identity引用
        去掉Identity节点
        最后再做一个检查
        """
        # 去掉对引用不存在的节点，并且给出警告
        for nodeName in self.sorted:
            input_norm = self.nodedict[nodeName].input_norm
            new_input_norm = []
            for i, inputNodeName in enumerate(input_norm):
                if inputNodeName not in self.sorted:
                    print("input %d %s of node %s not exist! deleted" % (i, inputNodeName, nodeName))
                    #new_input_norm.append(None)

                elif self.nodedict[inputNodeName].op == 'Identity':
                    tmp_input = self.nodedict[inputNodeName].input_norm[0]
                    if tmp_input not in self.sorted:
                        print("input %d %s of identity node %s not exist! deleted" % (0, tmp_input, inputNodeName))
                        #new_input_norm.append(None)
                    else:
                        new_input_norm.append(tmp_input)
                else:
                    new_input_norm.append(inputNodeName)
            self.nodedict[nodeName].input_norm = new_input_norm


        # 去掉Identity节点
        new_sorted = []
        new_nodedict = OrderedDict()
        for nodeName in self.sorted:
            node = self.nodedict[nodeName]
            op = node.op
            # print(op)
            if 'Identity' == op:
                pass
            #elif 'Assert' == op:
            #    pass
            else:
                new_sorted.append(nodeName)
                new_nodedict[nodeName] = node


        self.sorted = new_sorted
        self.nodedict = new_nodedict

        # 最后再做一次检查
        for nodeName in self.sorted:
            node = self.nodedict[nodeName]
            for inputNodeName in node.input_norm:
                assert inputNodeName in self.sorted

    def assignLocalNumber(self):
        from collections import defaultdict
        counter = defaultdict(int)
        for nodeName in self.sorted:
            node = self.nodedict[nodeName]
            op = node.op
            # print(op)
            node.lnum = counter[op]
            counter[op] += 1

    def getOpList(self):
        # you can filter some nodes here
        oplist = []
        mygraph = self
        for nodeName in mygraph.sorted:
            op = mygraph.nodedict[nodeName].op
            if op != 'Const':
                oplist.append(nodeName)
        return oplist

    def generateDot(self, dotfn):
        f = open(dotfn, 'w')
        f.write('digraph convnet {\n')

        oplist = self.getOpList()

        for nodeName in oplist:
            node = self.nodedict[nodeName]
            op = node.op
            #print(op, node.name)
            assert hasattr(ncnnops, op), op + ' not found'
            obj = getattr(ncnnops, op)(self, nodeName)
            for inVarName, inNode in zip(obj.inVarNames, obj.inNodes):
                if inNode.name in oplist:
                    f.write('%s -> %s;\n' % (inVarName.strip('_out'), obj.opName))
        f.write('}')
        f.close()
        import subprocess
        cmd = 'dot -T png -o %s.png %s' % (dotfn, dotfn)
        subprocess.call(cmd, shell=True)
