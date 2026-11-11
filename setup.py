from setuptools import setup

setup(
    name="evidence-packager",
    version="0.1.0",
    description="审查证据资料精确打包（Playwright 驱动本地合成门户）",
    python_requires=">=3.11",
    packages=["packager"],
    install_requires=["playwright==1.56.0"],
)
