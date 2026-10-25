// Valid program: all three loop types, nesting, break/continue
int n = 5;
int sum = 0;

for (int i = 0; i < n; i++) {
    sum += i;
}

int j = 10;
while (j > 0) {
    j -= 2;
    if (j == 4) continue;
    if (j < 2) break;
}

int k = 0;
do {
    k++;
} while (k < 3);

for (int a = 0; a < 3; a++) {
    for (int b = 3; b > 0; b--) {
        sum = sum + a * b;
    }
}

for (;;) {
    sum--;
    if (sum < 0) break;
}
