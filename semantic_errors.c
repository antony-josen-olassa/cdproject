int i = 0;
int n = 10;
float f = 2.5;

for (i = 0; i < n; i--) { }              // wrong direction: i decreases

for (int j = 0; j < 10; j + 1) { }       // update has no effect + empty body

while (i = 5) { i++; }                   // assignment in condition

while (n > 0) { i++; }                   // condition variable n never changes

for (int q = 0; q < 10; q++) { total = total + q; }   // undeclared 'total'

for (int i = 0; i < 3; i++); { n++; }    // stray semicolon (empty body)

while (1) { i++; }                       // infinite loop, no break

int m = 7 % 0;                           // division by zero
int r = f % 2;                           // % on float
break;                                   // break outside loop

int u;
for (int t = u; t < 4; t++) { }          // uninitialized use
