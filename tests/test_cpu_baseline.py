import unittest

from scripts.check_cpu_baseline import check_commands


class CpuBaselineTests(unittest.TestCase):
    def test_allows_avx2_baseline_and_cuda_gpu_architecture(self):
        check_commands([{'file': 'core.cpp', 'command':
                         'g++ -march=x86-64-v3 -mtune=generic -mavx2 -mfma -DHAS_AVX2_SUPPORT -c core.cpp'},
                        {'file': 'kernel.cu', 'arguments':
                         ['nvcc', '-arch=sm_75', '-Xcompiler=-march=x86-64-v3,-mtune=generic', 'kernel.cu']}])

    def test_rejects_higher_cpu_requirements(self):
        for flag in ('-march=native', '-march=x86-64-v4', '-march=icelake-server',
                     '-mavx512f', '-mavx512bw', '-mavxvnni', '-msha', '-maes', '-mtune=native',
                     '-Xcompiler=-march=native,-mtune=generic'):
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'core.cpp'):
                check_commands([{'file': 'core.cpp', 'command': f'g++ {flag} -c core.cpp'}])

    def test_catches_target_flags_overriding_a_generic_global_flag(self):
        with self.assertRaisesRegex(ValueError, 'march=native'):
            check_commands([{'file': 'core.cpp', 'arguments':
                             ['g++', '-march=x86-64-v3', '-march=native', '-c', 'core.cpp']}])

    def test_rejects_an_empty_compile_database(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            check_commands([])


if __name__ == '__main__':
    unittest.main()
