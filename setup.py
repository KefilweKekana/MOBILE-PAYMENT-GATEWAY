from setuptools import setup, find_packages

with open("requirements.txt") as f:
    install_requires = f.read().strip().split("\n")

with open("README.md") as f:
    long_description = f.read()

setup(
    name="mobile_payments",
    version="1.0.0",
    description="ERPNext Mobile Payment Gateway Integration (WaafiPay & Edahab)",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="Mobile Payments Team",
    author_email="dev@mobilepayments.so",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    install_requires=install_requires,
    python_requires=">=3.8",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Framework :: Frappe",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Office/Business :: Financial",
    ],
)
